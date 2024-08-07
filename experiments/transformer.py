"""transformer neural network module"""

from typing import Callable, Optional

from compyute.base_tensor import Tensor
from compyute.dtypes import Dtype, _DtypeLike
from compyute.nn.functional import dropout, softmax
from compyute.nn.modules.activations import _ActivationLike, get_activation
from compyute.nn.modules.containers import Container, Residual, Sequential
from compyute.nn.modules.embedding import Embedding
from compyute.nn.modules.linear import Linear
from compyute.nn.modules.module import Module
from compyute.nn.modules.normalization import Layernorm
from compyute.nn.modules.regularization import Dropout
from compyute.nn.parameter import Buffer, Parameter
from compyute.random.random import normal, uniform
from compyute.tensor_functions.creating import arange, concatenate, split, zeros_like


class Transformer(Container):
    r"""Docoder-only transformer model.

    Parameters
    ----------
    n_embeddings : int
        Number of embedding vectors.
    embedding_dim : int
        Number of embedding dimensions.
    feedforward_channels : int
        Number of channels of the hidden layer in the feed forward block.
    n_heads : int
        Number of attention heads.
    sequence_length : int
        Length of the input sequence.
    mask : Tensor, optional
        Mask for the attention. Defaults to ``None``.
    dropout_p : float, optional
        Dropout probability. Defaults to ``0.2``.
    activation : _ActivationLike
        Activation function to use in the feedforward blocks. Defaults to ``relu``.
        See :ref:`activations` for more details.
    attention_bias : bool, optional
        Whether to use bias values in the attention heads. Defaults to ``True``.
    feedforward_bias : bool, optional
        Whether to use bias values in the feedforward block. Defaults to ``True``.
    layernorm_eps : float, optional
        Constant for numerical stability. Defaults to ``1e-5``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to :class:`compyute.float32`.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.
    training: bool, optional
        Whether the module should be in training mode. Defaults to ``False``.


    .. note::
        All weights are initialized from :math:`\mathcal{U}(-k, k)`, where
        :math:`k = \sqrt{\frac{1}{C_{in} * k * k}}`. Biases are initialized as zeros.
    """

    def __init__(
        self,
        n_embeddings: int,
        embedding_dim: int,
        feedforward_channels: int,
        n_heads: int,
        n_layers: int,
        sequence_length: int,
        mask: Optional[Tensor] = None,
        dropout_p: float = 0.2,
        activation: _ActivationLike = "relu",
        attention_bias: bool = True,
        feedforward_bias: bool = True,
        layernorm_eps: float = 1e-5,
        dtype: _DtypeLike = Dtype.FLOAT32,
        label: Optional[str] = None,
        training: bool = False,
    ) -> None:

        block_kwargs = {
            "in_channels": embedding_dim,
            "feedforward_channels": feedforward_channels,
            "n_heads": n_heads,
            "mask": mask,
            "dropout_p": dropout_p,
            "out_proj_std": 0.02 * (2 * n_layers) ** -0.5,
            "activation": activation,
            "attention_bias": attention_bias,
            "feedforward_bias": feedforward_bias,
            "layernorm_eps": layernorm_eps,
            "dtype": dtype,
            "label": "TransformerBlock",
            "training": training,
        }

        # Embeddings
        self.token_emb = Embedding(n_embeddings, embedding_dim, dtype, "TokenEmbedding", training)
        self.token_emb.w = Parameter(normal((n_embeddings, embedding_dim), std=0.02, dtype=dtype))

        self.pos_emb = Embedding(sequence_length, embedding_dim, dtype, "PosEmbedding", training)
        self.pos_emb.w = Parameter(normal((n_embeddings, embedding_dim), std=0.01, dtype=dtype))

        # Transformer blocks
        self.blocks = [TransformerBlock(**block_kwargs) for _ in range(n_layers)]

        # Language model head
        self.ln = Layernorm((embedding_dim,), layernorm_eps, dtype, training=training)
        self.lm_head = Linear(embedding_dim, n_embeddings, False, dtype, "LmHead", training)
        self.lm_head.w = self.token_emb.w  # weight sharing

        modules = [self.token_emb, self.pos_emb, *self.blocks, self.ln, self.lm_head]
        super().__init__(*modules, label=label, training=training)

    def forward(self, x: Tensor) -> Tensor:
        pos = arange(x.shape[-1], device=x.device)
        x = self.token_emb(x) + self.pos_emb(pos)
        for block in self.blocks:
            x = block(x)
        y = self.lm_head(self.ln(x))

        def _backward(dy: Tensor) -> Tensor:
            dy = self.ln.backward(self.lm_head.backward(dy))
            for module in reversed(self.blocks):
                dy = module.backward(dy)
            self.token_emb.backward(dy)
            self.pos_emb.backward(dy)
            return zeros_like(x)  # dummy output

        self._backward = _backward

        return y


class TransformerBlock(Sequential):
    """Decoder-only transformer block consisting of a multi head attention block
    and a feed forward block.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    feedforward_channels : int
        Number of channels of the hidden layer in the feed forward block.
    n_heads : int
        Number of attention heads.
    mask : Tensor, optional
        Mask for the attention. Defaults to ``None``.
    dropout_p : float, optional
        Dropout probability. Defaults to ``0.2``.
    out_proj_std : float, optional
        Weight scale factor for the output projection to compensate the growing
        variance of the residual block. Defaults to ``0.02``.
    activation : _ActivationLike
        Activation function to use in the feedforward blocks. Defaults to ``relu``.
        See :ref:`activations` for more details.
    attention_bias : bool, optional
        Whether to use bias values in the input and output projections
        of the multi head attention block. Defaults to ``True``.
    feedforward_bias : bool, optional
        Whether to use bias values in the feedforward block. Defaults to ``True``.
    layernorm_eps : float, optional
        Constant for numerical stability. Defaults to ``1e-5``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to :class:`compyute.float32`.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.
    training: bool, optional
        Whether the module should be in training mode. Defaults to ``False``.
    """

    def __init__(
        self,
        in_channels: int,
        feedforward_channels: int,
        n_heads: int,
        mask: Optional[Tensor] = None,
        dropout_p: float = 0.2,
        out_proj_std: float = 0.02,
        activation: _ActivationLike = "relu",
        attention_bias: bool = True,
        feedforward_bias: bool = True,
        layernorm_eps: float = 1e-5,
        dtype: _DtypeLike = Dtype.FLOAT32,
        label: Optional[str] = None,
        training: bool = False,
    ) -> None:
        ln_kwargs = {
            "normalized_shape": (in_channels,),
            "eps": layernorm_eps,
            "dtype": dtype,
            "training": training,
        }

        mha_kwargs = {
            "in_channels": in_channels,
            "n_heads": n_heads,
            "mask": mask,
            "dropout_p": dropout_p,
            "out_proj_std": out_proj_std,
            "bias": attention_bias,
            "dtype": dtype,
            "training": training,
        }

        feedforward_kwargs = {
            "in_channels": in_channels,
            "h_channels": feedforward_channels,
            "dropout_p": dropout_p,
            "out_proj_std": out_proj_std,
            "activation": activation,
            "bias": feedforward_bias,
            "dtype": dtype,
            "training": training,
        }

        attention_block = Residual(
            Layernorm(**ln_kwargs),
            MultiHeadAttention(**mha_kwargs),
            training=training,
        )

        feedforward_block = Residual(
            Layernorm(**ln_kwargs),
            FeedForward(**feedforward_kwargs),
            training=training,
        )

        super().__init__(attention_block, feedforward_block, label=label, training=training)


class FeedForward(Sequential):
    """FeedForward block for transformers.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    h_channels : int
        Number of channels of the hidden layer.
    dropout_p : float, optional
        Dropout probability. Defaults to ``0``.
    out_proj_std : float, optional
        Weight scale factor for the output projection to compensate the growing
        variance of the residual block. Defaults to ``0.02``.
    activation : _ActivationLike
        Activation function to use. Defaults to ``relu``.
        See :ref:`activations` for more details.
    bias : bool, optional
        Whether to use bias values. Defaults to ``True``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to :class:`compyute.float32`.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.
    training: bool, optional
        Whether the module should be in training mode. Defaults to ``False``.
    """

    def __init__(
        self,
        in_channels: int,
        h_channels: int,
        dropout_p: float = 0,
        out_proj_std: float = 0.02,
        activation: _ActivationLike = "relu",
        bias: bool = True,
        dtype: _DtypeLike = Dtype.FLOAT32,
        label: Optional[str] = None,
        training: bool = False,
    ) -> None:
        lin = Linear(in_channels, h_channels, bias, dtype, training=training)
        act = get_activation(activation)(training=training)
        out_proj = Linear(h_channels, in_channels, bias, dtype, "OutProj", training)
        w = uniform((in_channels, h_channels), -out_proj_std, out_proj_std, dtype)
        out_proj.w = Parameter(w)
        drop = Dropout(dropout_p, training=training) if dropout_p > 0 else None

        modules = [lin, act, out_proj] + [drop] if drop is not None else []
        super().__init__(*modules, label=label, training=training)


class MultiHeadAttention(Container):
    r"""Multi Head Attention.

    .. math::
        \begin{array}{ll} \\
            Q = xW_q^T \\
            K = xW_q^T \\
            V = xW_q^T \\
            MultiHeadAttention(x) = concatenate(Attention_1(Q, K, V), ..., Attention_n(Q, K, V))W_o^T \\
        \end{array}
    
    where

    .. math::
            Attention(Q, K, V) = softmax(\frac{QK^T}{\sqrt{N}}) \cdot V

    Shapes:
        - Input :math:`(B, S, C_{in})`
        - Output :math:`(B, S, C_{in})`
    where
        - :math:`B` ... batch axis
        - :math:`S` ... sequence
        - :math:`C_{in}` ... input channels
        - :math:`N` ... number of attention heads 

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    n_heads : int
        Number of attention heads.
    mask : Tensor, optional
        Mask for the attention. Defaults to ``None``.
    dropout_p : float, optional
        Dropout probability. Defaults to ``0``.
    out_proj_std : float, optional
        Weight scale factor for the output projection to compensate the growing
        variance of the residual block. Defaults to ``0.02``.
    bias : bool, optional
        Whether to use bias values to input and output projection. Defaults to ``True``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to :class:`compyute.float32`.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.
    training: bool, optional
        Whether the module should be in training mode. Defaults to ``False``.


    .. note::
        All weights are initialized from :math:`\mathcal{U}(-k, k)`, where
        :math:`k = \sqrt{\frac{1}{C_{in} * k * k}}`. Biases are initialized as zeros.
    """

    def __init__(
        self,
        in_channels: int,
        n_heads: int,
        mask: Optional[Tensor] = None,
        dropout_p: float = 0,
        out_proj_std: float = 0.02,
        bias: bool = True,
        dtype: _DtypeLike = Dtype.FLOAT32,
        label: Optional[str] = None,
        training: bool = False,
    ) -> None:
        if in_channels % n_heads != 0:
            raise ValueError("Number of input channels must be divisible by number of heads.")

        self.n_heads = n_heads
        self.mask = Buffer(mask) if mask is not None else None
        self.dropout_p = dropout_p
        self.dtype = Dtype(dtype)

        # Input projection
        self.query = Linear(in_channels, in_channels, bias, self.dtype, "QueryProj", training)
        self.key = Linear(in_channels, in_channels, bias, self.dtype, "KeyProj", training)
        self.value = Linear(in_channels, in_channels, bias, self.dtype, "ValueProj", training)

        # Output projection
        self.out_proj = Linear(in_channels, in_channels, bias, self.dtype, "OutProj", training)
        w = uniform((in_channels, in_channels), -out_proj_std, out_proj_std, self.dtype)
        self.out_proj.w = Parameter(w)

        modules: list[Module] = [self.query, self.key, self.value, self.out_proj]

        # Optional dropout
        if dropout_p > 0:
            self.dropout = Dropout(dropout_p, training=training)
            modules.append(self.dropout)

        super().__init__(*modules, label=label, training=training)

    def forward(self, x: Tensor) -> Tensor:
        self._check_dims(x, [3])
        x = x.to_type(self.dtype)
        head_grad_functions, ys = [], []
        sdp_dropout_p = self.dropout_p if self._training else 0

        # input projection (B, S, C_in) -> (B, S, C_in)
        q, k, v = self.query(x), self.key(x), self.value(x)

        # split projections into heads (B, S, C_in) -> (B, S, C_in // n_heads)
        q_heads = split(q, self.n_heads)
        k_heads = split(k, self.n_heads)
        v_heads = split(v, self.n_heads)

        # multi head attention: compute attention weights for each head, concat results
        for q_head, k_head, v_head in zip(q_heads, k_heads, v_heads):
            y_head, attn_grad_fn = scaled_dot_product_attention(
                q_head, k_head, v_head, self.mask, sdp_dropout_p, self._training
            )
            ys.append(y_head)
            head_grad_functions.append(attn_grad_fn)
        y = concatenate(ys)

        # output projection (B, S, C_in) -> (B, S, C_in)
        y = self.out_proj(y)
        if self.dropout_p > 0:
            y = self.dropout(y)

        if self._training:

            def _backward(dy: Tensor) -> Tensor:
                dy = dy.to_type(self.dtype)

                if self.dropout_p > 0:
                    dy = self.dropout.backward(dy)
                dy = self.out_proj.backward(dy)

                dy_splits = split(dy, self.n_heads)
                dq_heads, dk_heads, dv_heads = [], [], []
                for grad_fn, dy_head in zip(head_grad_functions, dy_splits):
                    dq_head, dk_head, dv_head = grad_fn(dy_head)
                    dq_heads.append(dq_head)
                    dk_heads.append(dk_head)
                    dv_heads.append(dv_head)

                dq = concatenate(dq_heads)
                dk = concatenate(dk_heads)
                dv = concatenate(dv_heads)

                dx1 = self.query.backward(dq)
                dx2 = self.key.backward(dk)
                dx3 = self.value.backward(dv)

                return dx1 + dx2 + dx3

            self._backward = _backward

        return y


def scaled_dot_product_attention(
    q: Tensor,
    k: Tensor,
    v: Tensor,
    mask: Optional[Tensor] = None,
    dropout_p: float = 0,
    return_grad_fn: bool = False,
) -> tuple[Tensor, Optional[Callable[[Tensor], tuple[Tensor, Tensor, Tensor]]]]:
    """Computes the scaled dot product attention scores.

    Parameters
    ----------
    q : Tensor
        Query tensor.
    k : Tensor
        Key tensor.
    v : Tensor
        Value tensor.
    mask : Tensor, optional
        Mask for the attention. Defaults to ``None``.
    dropout_p : float, optional
        Dropout probability of attention weights. Defaults to ``0``.
    return_grad_fn : bool, optional
        Whether to also return the according gradient function. Defaults to ``False``.

    Returns
    -------
    Tensor
        Output tensor.
    Callable[[Tensor], tuple[Tensor, Tensor, Optional[Tensor]]], optional
        Gradient function.

    See Also
    ----------
    :class:`compyute.nn.MultiHeadAttention`
    """
    scale_factor = q.shape[-1] ** -0.5

    qk = q @ k.T * scale_factor
    if mask is not None:
        qk += mask[: q.shape[1], : q.shape[1]]  # truncate mask for smaller context sizes
    attn_weights, sm_grad_fn = softmax(qk, return_grad_fn)
    if dropout_p > 0:
        attn_weights, drouput_grad_fn = dropout(attn_weights, dropout_p, return_grad_fn)
    y = attn_weights @ v

    if return_grad_fn:

        def grad_fn(dy: Tensor) -> tuple[Tensor, Tensor, Tensor]:
            # attention gradients
            dattn_weights = dy @ v.T
            if dropout_p > 0:
                dattn_weights = drouput_grad_fn(dattn_weights)
            dqk = sm_grad_fn(dattn_weights) * scale_factor

            # query, key, value gradients
            dq = dqk @ k
            dk = dqk.T @ q
            dv = attn_weights.T @ dy

            return dq, dk, dv

        return y, grad_fn

    return y, None
