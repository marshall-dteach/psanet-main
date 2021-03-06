# copyright (c) 2022 PaddlePaddle Authors. All Rights Reserve.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import paddle
import paddle.nn as nn
import paddle.nn.functional as F

from paddleseg.models import layers
from paddleseg.utils import utils
from paddleseg.cvlibs import manager, param_init


class AttenHead(nn.Layer):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        bot_ch = 256
        self.conv_bn_re0 = layers.ConvBNReLU(
            in_ch, bot_ch, kernel_size=3, padding=1, bias_attr=False)
        self.conv_bn_re1 = layers.ConvBNReLU(
            bot_ch, bot_ch, kernel_size=3, padding=1, bias_attr=False)
        self.conv2 = nn.Conv2D(bot_ch, out_ch, kernel_size=1, bias_attr=False)
        self.sig = nn.Sigmoid()

    def forward(self, x):
        x = self.conv_bn_re0(x)
        x = self.conv_bn_re1(x)
        x = self.conv2(x)
        x = self.sig(x)
        return x


class SpatialGather_Module(nn.Layer):
    """
        Aggregate the context features according to the initial
        predicted probability distribution.
        Employ the soft-weighted method to aggregate the context.

        Output:
          The correlation of every class map with every feature map
          shape = [n, num_feats, num_classes, 1]


    """

    def __init__(self, cls_num=0, scale=1):
        super().__init__()
        self.cls_num = cls_num
        self.scale = scale

    def forward(self, feats, probs):
        batch_size, c = paddle.shape(probs).numpy()[0:2]
        probs = probs.reshape((batch_size, c, -1))
        feats = feats.reshape((batch_size, paddle.shape(feats).numpy()[1], -1))
        feats = feats.transpose((0, 2, 1))
        probs = F.softmax(self.scale * probs, axis=2)
        ocr_context = paddle.matmul(probs, feats)
        ocr_context = ocr_context.transpose((0, 2, 1)).unsqueeze(3)
        return ocr_context


class ObjectAttentionBlock(nn.Layer):
    '''
    The basic implementation for object context block
    Input:
        N X C X H X W
    Parameters:
        in_channels       : the dimension of the input feature map
        key_channels      : the dimension after the key/query transform
        scale             : choose the scale to downsample the input feature
                            maps (save memory cost)
    Return:
        N X C X H X W
    '''

    def __init__(self, in_channels, key_channels, scale=1):
        super().__init__()
        self.scale = scale
        self.in_channels = in_channels
        self.key_channels = key_channels
        self.pool = nn.MaxPool2D(kernel_size=(scale, scale))
        self.f_pixel = layers.SpatialConvBNReLU(
            self.in_channels,
            self.key_channels,
            kernel_size=1,
            padding=0,
            bias_attr=False)
        self.f_object = layers.SpatialConvBNReLU(
            self.in_channels,
            self.key_channels,
            kernel_size=1,
            padding=0,
            bias_attr=False)
        self.f_down = layers.ConvBNReLU(
            self.in_channels,
            self.key_channels,
            kernel_size=1,
            padding=0,
            bias_attr=False)
        self.f_up = layers.ConvBNReLU(
            self.key_channels,
            self.in_channels,
            kernel_size=1,
            padding=0,
            bias_attr=False)

    def forward(self, x, proxy):
        batch_size, _, h, w = paddle.shape(x).numpy()
        if self.scale > 1:
            x = self.pool(x)

        query = self.f_pixel(x).reshape((batch_size, self.key_channels, -1))
        query = query.transpose((0, 2, 1))
        key = self.f_object(proxy).reshape((batch_size, self.key_channels, -1))
        value = self.f_down(proxy).reshape((batch_size, self.key_channels, -1))
        value = value.transpose((0, 2, 1))
        sim_map = paddle.matmul(query, key)
        sim_map = (self.key_channels**-.5) * sim_map
        sim_map = F.softmax(sim_map, axis=-1)
        context = paddle.matmul(sim_map, value)
        context = context.transpose((0, 2, 1))
        context = context.reshape((batch_size, self.key_channels,
                                   *paddle.shape(x).numpy()[2:]))
        context = self.f_up(context)
        if self.scale > 1:
            context = F.interpolate(context, size=(h, w), mode='bilinear')
        return context


class SpatialOCR_Module(nn.Layer):
    def __init__(self,
                 in_channels,
                 key_channels,
                 out_channels,
                 scale=1,
                 dropout=0.1):
        super().__init__()
        self.object_context_block = ObjectAttentionBlock(in_channels,
                                                         key_channels, scale)
        _in_channels = 2 * in_channels
        self.conv_bn_dropout = nn.Sequential(
            layers.ConvBNReLU(
                _in_channels,
                out_channels,
                kernel_size=1,
                padding=0,
                bias_attr=False),
            nn.Dropout2D(dropout))

    def forward(self, feats, proxy_feats):
        context = self.object_context_block(feats, proxy_feats)
        output = paddle.concat([context, feats], 1)
        output = self.conv_bn_dropout(output)
        return output


class OCRHead(nn.Layer):
    def __init__(self, num_classes, in_channels):
        super().__init__()

        ocr_mid_channels = 512
        ocr_key_channels = 256
        self.indices = [-2, -1] if len(in_channels) > 1 else [-1, -1]
        high_level_ch = in_channels[self.indices[1]]
        self.conv3x3_ocr = layers.ConvBNReLU(
            high_level_ch, ocr_mid_channels, kernel_size=3, stride=1, padding=1)
        self.ocr_gather_head = SpatialGather_Module(num_classes)
        self.ocr_distri_head = SpatialOCR_Module(
            in_channels=ocr_mid_channels,
            key_channels=ocr_key_channels,
            out_channels=ocr_mid_channels,
            scale=1,
            dropout=0.05, )
        self.cls_head = nn.Conv2D(
            ocr_mid_channels,
            num_classes,
            kernel_size=1,
            stride=1,
            padding=0,
            bias_attr=True)
        self.aux_head = nn.Sequential(
            layers.ConvBNReLU(
                high_level_ch,
                high_level_ch,
                kernel_size=1,
                stride=1,
                padding=0),
            nn.Conv2D(
                high_level_ch,
                num_classes,
                kernel_size=1,
                stride=1,
                padding=0,
                bias_attr=True))
        self.init_weight()

    def forward(self, high_level_features):
        high_level_features = high_level_features[0]
        feats = self.conv3x3_ocr(high_level_features)
        aux_out = self.aux_head(high_level_features)
        context = self.ocr_gather_head(feats, aux_out)
        ocr_feats = self.ocr_distri_head(feats, context)
        cls_out = self.cls_head(ocr_feats)
        return cls_out, aux_out, ocr_feats

    def init_weight(self):
        """Initialize the parameters of model parts."""
        for sublayer in self.sublayers():
            if isinstance(sublayer, nn.Conv2D):
                param_init.normal_init(sublayer.weight, std=0.001)
            elif isinstance(sublayer, (nn.BatchNorm, nn.SyncBatchNorm)):
                param_init.constant_init(sublayer.weight, value=1.0)
                param_init.constant_init(sublayer.bias, value=0.0)


@manager.MODELS.add_component
class MscaleOCR(nn.Layer):
    """
    The MSOCRNet implementation based on PaddlePaddle.

    The original article refers to
    Zhao, Hengshuang, et al. "Pyramid scene parsing network"
    (https://openaccess.thecvf.com/content_cvpr_2017/papers/Zhao_Pyramid_Scene_Parsing_CVPR_2017_paper.pdf).

    Args:
        num_classes (int): The unique number of target classes.
        backbone (Paddle.nn.Layer): Backbone network, currently support Resnet50/101.
        backbone_indices (tuple, optional): Two values in the tuple indicate the indices of output of backbone.
        pp_out_channels (int, optional): The output channels after Pyramid Pooling Module. Default: 1024.
        bin_sizes (tuple, optional): The out size of pooled feature maps. Default: (1,2,3,6).
        enable_auxiliary_loss (bool, optional): A bool value indicates whether adding auxiliary loss. Default: True.
        align_corners (bool, optional): An argument of F.interpolate. It should be set to False when the feature size is even,
            e.g. 1024x512, otherwise it is True, e.g. 769x769. Default: False.
        pretrained (str, optional): The path or url of pretrained model. Default: None.
    """

    def __init__(self,
                 num_classes,
                 backbone,
                 backbone_indices=[0],
                 preteained=None):
        super(MscaleOCR, self).__init__()
        self.backbone = backbone
        self.pretrained = preteained
        self.backbone_indices = backbone_indices
        in_channels = [self.backbone.feat_channels[i] for i in backbone_indices]
        self.ocr = OCRHead(num_classes, in_channels)
        self.scale_attn = AttenHead(in_ch=512, out_ch=1)
        self.init_weight()

    def _fwd(self, x):
        x_size = paddle.shape(x)[2:]
        high_level_features = self.backbone(x)
        cls_out, aux_out, ocr_mid_feats = self.ocr(high_level_features)
        attn = self.scale_attn(ocr_mid_feats)

        aux_out = F.interpolate(aux_out, size=x_size, mode='bilinear')
        cls_out = F.interpolate(cls_out, size=x_size, mode='bilinear')
        attn = F.interpolate(attn, size=x_size, mode='bilinear')

        return {'cls_out': cls_out, 'aux_out': aux_out, 'logit_attn': attn}

    def nscale_forward(self, inputs, scales):
        x_1x = inputs
        scales = sorted(scales, reverse=True)
        pred = None
        aux = None
        output_dict = {}
        for s in scales:
            x = F.interpolate(x_1x, scale_factor=s, mode='bilinear')
            outs = self._fwd(x)
            cls_out = outs['cls_out']
            attn_out = outs['logit_attn']
            aux_out = outs['aux_out']
            key_pred = 'pred_' + str(float(s)).replace('.', '') + 'x'
            output_dict[key_pred] = cls_out
            if s != 2.0:
                key_attn = 'attn_' + str(float(s)).replace('.', '') + 'x'
                output_dict[key_attn] = attn_out
            if pred is None:
                pred = cls_out
                aux = aux_out
            elif s >= 1.0:
                pred = F.interpolate(
                    pred, size=paddle.shape(cls_out)[2:4], mode='bilinear')
                pred = attn_out * cls_out + (1 - attn_out) * pred
                aux = F.interpolate(
                    aux, size=paddle.shape(cls_out)[2:4], mode='bilinear')
                aux = attn_out * aux_out + (1 - attn_out) * aux
            else:
                cls_out = attn_out * cls_out
                aux_out = attn_out * aux_out
                cls_out = F.interpolate(
                    cls_out, size=paddle.shape(pred)[2:4], mode='bilinear')
                aux_out = F.interpolate(
                    aux_out, size=paddle.shape(pred)[2:4], mode='bilinear')
                attn_out = F.interpolate(
                    attn_out, size=paddle.shape(pred)[2:4], mode='bilinear')
                pred = cls_out + (1 - attn_out) * pred
                aux = aux_out + (1 - attn_out) * aux
        logit_list = [aux, pred] if self.training else [pred]
        return logit_list

    def two_scale_forward(self, inputs):
        x_lo = F.interpolate(inputs, scale_factor=0.5, mode='bilinear')
        lo_outs = self._fwd(x_lo)
        pred_05x = lo_outs['cls_out']
        p_lo = pred_05x
        aux_lo = lo_outs['aux_out']
        logit_attn = lo_outs['logit_attn']

        hi_outs = self._fwd(inputs)
        pred_10x = hi_outs['cls_out']
        p_1x = pred_10x
        aux_1x = hi_outs['aux_out']

        p_lo = logit_attn * p_lo
        aux_lo = logit_attn * aux_lo
        p_lo = F.interpolate(
            p_lo, size=paddle.shape(p_1x)[2:4], mode='bilinear')
        aux_lo = F.interpolate(
            aux_lo, size=paddle.shape(p_1x)[2:4], mode='bilinear')
        logit_attn = F.interpolate(
            logit_attn, size=paddle.shape(p_1x)[2:4], mode='bilinear')
        joint_pred = p_lo + (1 - logit_attn) * p_1x
        joint_aux = aux_lo + (1 - logit_attn) * aux_1x
        if self.training:
            scaled_pred_05x = F.interpolate(
                pred_05x, size=paddle.shape(p_1x)[2:4], mode='bilinear')
            logit_list = [joint_aux, joint_pred, scaled_pred_05x, pred_10x]
        else:
            logit_list = [joint_pred]
        return logit_list

    def init_weight(self):
        if self.pretrained is not None:
            utils.load_entire_model(self, self.pretrained)

    def forward(self, inputs):
        if [0.5, 1.0, 2.0] and not self.training:
            return self.nscale_forward(inputs, [0.5, 1.0, 2.0])
        return self.two_scale_forward(inputs)
