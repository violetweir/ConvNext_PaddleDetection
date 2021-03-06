import paddle 
import paddle.fluid as fluid
import paddle.nn.functional as F
import paddle.nn as nn
from paddle import ParamAttr

from ppdet.modeling.shape_spec import ShapeSpec

from ppdet.core.workspace import register, serializable


__all__ = ['CSPConvNext_V2']

trunc_normal_ = nn.initializer.TruncatedNormal(std=0.02)
zeros_ = nn.initializer.Constant(value=0.0)
ones_ = nn.initializer.Constant(value=1.0)

class Identity(nn.Layer):

    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x

def drop_path(x, drop_prob=0.0, training=False):
    if drop_prob == 0.0 or not training:
        return x
    keep_prob = paddle.to_tensor(1 - drop_prob)
    shape = (paddle.shape(x)[0], ) + (1, ) * (x.ndim - 1)
    random_tensor = keep_prob + paddle.rand(shape, dtype=x.dtype)
    random_tensor = paddle.floor(random_tensor)  # binarize
    output = x.divide(keep_prob) * random_tensor
    return output


class DropPath(nn.Layer):

    def __init__(self, drop_prob=None):
        super(DropPath, self).__init__()
        self.drop_prob = drop_prob

    def forward(self, x):
        return drop_path(x, self.drop_prob, self.training)


class Block(nn.Layer):
    """ ConvNeXt Block. There are two equivalent implementations:
    (1) DwConv -> LayerNorm (channels_first) -> 1x1 Conv -> GELU -> 1x1 Conv; all in (N, C, H, W)
    (2) DwConv -> Permute to (N, H, W, C); LayerNorm (channels_last) -> Linear -> GELU -> Linear; Permute back
    Args:
        dim (int): Number of input channels.
        drop_path (float): Stochastic depth rate. Default: 0.0
        layer_scale_init_value (float): Init value for Layer Scale. Default: 1e-6.
    """

    def __init__(self, dim, drop_path=0., layer_scale_init_value=1e-6):
        super().__init__()
        self.dwconv = nn.Conv2D(dim, dim, kernel_size=7, padding=3,
                                groups=dim)  # depthwise conv
        self.norm = nn.BatchNorm2D(dim, epsilon=1e-6)
        self.pwconv1 = nn.Conv2D(dim, dim*4, kernel_size=1, padding=0,)  # pointwise/1x1 convs, implemented with linear layers
        self.act = nn.Swish()
        self.pwconv2 = nn.Conv2D(4*dim, dim, kernel_size=1, padding=0,)

        self.gamma = paddle.create_parameter(
            shape=[dim],
            dtype='float32',
            default_initializer=nn.initializer.Constant(
                value=layer_scale_init_value)
        ) if layer_scale_init_value > 0 else None

        self.drop_path = DropPath(drop_path) if drop_path > 0. else Identity()

    def forward(self, x):
        input = x
        x = self.dwconv(x) 
        x = self.norm(x)
        x = self.pwconv1(x)
        x = self.act(x)
        x = self.pwconv2(x)
        # if self.gamma is not None:
        #     x = self.gamma * x

        x = input + self.drop_path(x)
        return x


class LayerNorm(nn.Layer):
    """ LayerNorm that supports two data formats: channels_last (default) or channels_first.
    The ordering of the dimensions in the inputs. channels_last corresponds to inputs with
    shape (batch_size, height, width, channels) while channels_first corresponds to inputs
    with shape (batch_size, channels, height, width).
    """

    def __init__(self,
                 normalized_shape,
                 epsilon=1e-6,
                 data_format="channels_last"):
        super().__init__()

        self.weight = paddle.create_parameter(shape=[normalized_shape],
                                              dtype='float32',
                                              default_initializer=ones_)

        self.bias = paddle.create_parameter(shape=[normalized_shape],
                                            dtype='float32',
                                            default_initializer=zeros_)

        self.epsilon = epsilon
        self.data_format = data_format
        if self.data_format not in ["channels_last", "channels_first"]:
            raise NotImplementedError
        self.normalized_shape = (normalized_shape, )

    def forward(self, x):
        if self.data_format == "channels_last":
            return F.layer_norm(x, self.normalized_shape, self.weight,
                                self.bias, self.epsilon)
        elif self.data_format == "channels_first":
            u = x.mean(1, keepdim=True)
            s = (x - u).pow(2).mean(1, keepdim=True)
            x = (x - u) / paddle.sqrt(s + self.epsilon)
            x = self.weight[:, None, None] * x + self.bias[:, None, None]
            return x


class L2Decay(fluid.regularizer.L2Decay):
    def __init__(self, coeff=0.0):
        super(L2Decay, self).__init__(coeff)

class EffectiveSELayer(nn.Layer):
    """ Effective Squeeze-Excitation
    From `CenterMask : Real-Time Anchor-Free Instance Segmentation` - https://arxiv.org/abs/1911.06667
    """

    def __init__(self, channels, act='hardsigmoid'):
        super(EffectiveSELayer, self).__init__()
        self.fc = nn.Conv2D(channels, channels, kernel_size=1, padding=0)
        self.act = nn.Hardsigmoid()

    def forward(self, x):
        x_se = x.mean((2, 3), keepdim=True)
        x_se = self.fc(x_se)
        return x * self.act(x_se)



class ConvBNLayer(nn.Layer):
    def __init__(self,
                 ch_in,
                 ch_out,
                 filter_size=3,
                 stride=1,
                 groups=1,
                 padding=0,
                 act=None):
        super(ConvBNLayer, self).__init__()

        self.conv = nn.Conv2D(
            in_channels=ch_in,
            out_channels=ch_out,
            kernel_size=filter_size,
            stride=stride,
            padding=padding,
            groups=groups,
            bias_attr=False)

        self.bn = nn.LayerNorm(
            ch_out,
            weight_attr=ParamAttr(regularizer=L2Decay(0.0)),
            bias_attr=ParamAttr(regularizer=L2Decay(0.0)))
        self.act = nn.GELU()

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        x = self.act(x)

        return x


class CSPStage(nn.Layer):
    def __init__(self,
                block_fn,
                ch_in,
                ch_out,
                n,
                stride,
                p_rates,
                layer_scale_init_value=1e-6,
                act=nn.GELU,
                attn='eca'):
        super().__init__()
        ch_mid = (ch_in+ch_out)//2
        if stride == 2:
            self.down = nn.Sequential(
                nn.BatchNorm2D(ch_in, epsilon=1e-6),
                nn.Conv2D(ch_in, ch_out, kernel_size=2, stride=2),
            )
        else:
            self.down = nn.Sequential(
                nn.BatchNorm2D(ch_in, epsilon=1e-6),
                nn.Conv2D(ch_in, ch_out, kernel_size=3, stride=1, padding=1),
            )

        self.conv1 = nn.Sequential(
                nn.Swish(),
                nn.Conv2D(ch_out, ch_out//2,1,1,0),
            )
        self.conv2 = nn.Sequential(
                nn.Swish(),
                nn.Conv2D(ch_out, ch_out//2,1,1,0),
            )
        self.blocks = nn.Sequential(*[
            block_fn(
                ch_out // 2, drop_path=p_rates[i],layer_scale_init_value=layer_scale_init_value)
            for i in range(n)
        ])
        if attn:
            self.attn = EffectiveSELayer(ch_out, act='hardsigmoid')
        else:
            self.attn = None
        self.conv3 = nn.Sequential(
                nn.Conv2D(ch_out,ch_out,1,1,0),
                nn.Swish())

    def forward(self, x):
        if self.down is not None:
            x = self.down(x)
        y1 = self.conv1(x)
        y2 = self.blocks(self.conv2(x))
        y = paddle.concat([y1, y2], axis=1)
        if self.attn is not None:
            y = self.attn(y)
        y = self.conv3(y)
        return y


@register
@serializable
class CSPConvNext_V2(nn.Layer):
    def __init__(
        self,
        in_chans=3,
        depths=[3, 3, 9, 3],
        dims=[64, 128,256,512,1024],
        drop_path_rate=0.,
        layer_scale_init_value=1e-6,
        stride=[1,2,2,2],
        return_idx=[1,2,3]
    ):
        super().__init__()
        self._out_strides = [4, 8, 16, 32]
        self._out_channels = dims[1:]
        self.out_channals = return_idx
        self.return_idx = return_idx
        self.Down_Conv = nn.Sequential(
            nn.Conv2D(3, dims[0], kernel_size=4, stride=4),
            nn.BatchNorm2D(dims[0], epsilon=1e-6))
        dp_rates = [
            x.item() for x in paddle.linspace(0, drop_path_rate, sum(depths))
        ]
        n = len(depths)

        self.stages = nn.Sequential(*[(str(i), CSPStage(
            Block, dims[i], dims[i + 1], depths[i], stride[i], dp_rates[sum(depths[:i]) : sum(depths[:i+1])],act=nn.GELU))
                                      for i in range(n)])
        self.norm = nn.LayerNorm(dims[-1], epsilon=1e-6) 

        self.apply(self._init_weights)


    def _init_weights(self, m):
        if isinstance(m, (nn.Conv2D, nn.Linear)):
            try:
                trunc_normal_(m.weight)
                zeros_(m.bias)
            except:
                print(m)

    @property
    def out_shape(self):
        return [
            ShapeSpec(
                channels=self._out_channels[i], stride=self._out_strides[i])
            for i in self.out_channals
        ]
    
    
    def forward(self, inputs):
        x = inputs['image']
        x = self.Down_Conv(x)
        outs = []
        for idx, stage in enumerate(self.stages):
            x = stage(x)
            if idx in self.return_idx:
                outs.append(x)
        return outs