"""transformer neural network module"""

import math
from typing import Optional

from compyute.nn.functional.activations import FSoftmax
from compyute.nn.functional.functions import Function, FunctionCache, PseudoCache
from compyute.nn.functional.regularizations import FDropout
from compyute.nn.modules.activations import ReLU
from compyute.nn.modules.embedding import Embedding
from compyute.nn.modules.linear import Linear
from compyute.nn.modules.module import Module, ModuleList, validate_input_axes
from compyute.nn.modules.normalization import LayerNorm
from compyute.nn.modules.regularization import Dropout
from compyute.nn.parameter import Buffer
from compyute.nn.utils.initializers import init_normal
from compyute.tensor_ops.creating import arange, concat, full, split, zeros
from compyute.tensor_ops.reshaping import insert_dim
from compyute.tensor_ops.selecting import triu
from compyute.tensor_ops.transforming import cos, exp, sin
from compyute.tensors import ShapeLike, Tensor
from compyute.typing import DType, int32


def get_causal_mask(shape: ShapeLike) -> Tensor:
    """Returns a causal mask used for the self-attention mechanism.

    Parameters
    ----------
    shape : ShapeLike
        Shape of the mask.

    Returns
    -------
    Tensor
        Causal mask.
    """
    return triu(full(shape, float("-inf")), d=1)


class Transformer(Module):
    r"""Docoder-only transformer model.

    Parameters
    ----------
    n_embeddings : int
        Number of embedding vectors.
    embedding_dim : int
        Number of embedding dimensions.
    ffwd_channels : int
        Number of channels of the hidden layer in the feed forward block.
    n_heads : int
        Number of attention heads.
    n_blocks : int
        Number of transformer blocks.
    max_seq_len : int
        Maximum possible length of the input sequence.
    mask : Tensor, optional
        Attention-mask. Defaults to ``None``.
        Must be a zeros-tensor with values of ```-inf`` indicating elements to be masked out.
    dropout_p : float, optional
        Dropout probability. Defaults to ``0.2``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to ``None``.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.


    .. note::
        Embeddings are initialized from :math:`\mathcal{N}(0, \sqrt{\frac{1}{C_{in}}})`.
        Linear layer weights are initialized from :math:`\mathcal{U}(-k, k)`, where
        :math:`k = \sqrt{\frac{1}{C_{in}}}`. Biases are initialized as zeros.

    .. note::
        Dropout is applied to the output of each residual block and to attention weights.

    .. note::
        The weights of the token embedding and the language model head are shared.

    .. note::
        Normalization is applied before weight layers within
        residual blocks (pre-weight-normalization).
    """

    def __init__(
        self,
        n_embeddings: int,
        embedding_dim: int,
        ffwd_channels: int,
        n_heads: int,
        n_blocks: int,
        max_seq_len: int,
        mask: Optional[Tensor] = None,
        dropout_p: float = 0.2,
        dtype: Optional[DType] = None,
        label: Optional[str] = None,
    ) -> None:
        super().__init__(label)

        # Embeddings
        self.token_emb = Embedding(n_embeddings, embedding_dim, dtype, "TokenEmbedding")
        self.pos_emb = Embedding(max_seq_len, embedding_dim, dtype, "PosEmbedding")
        init_normal(self.token_emb.w, self.pos_emb.w, std=1 / math.sqrt(embedding_dim))

        # Transformer blocks
        block_kwargs = {
            "in_channels": embedding_dim,
            "ffwd_channels": ffwd_channels,
            "n_heads": n_heads,
            "out_scale": 1 / math.sqrt(2 * n_blocks),
            "mask": mask,
            "dropout_p": dropout_p,
            "dtype": dtype,
        }
        self.blocks = ModuleList(
            TransformerBlock(**block_kwargs) for _ in range(n_blocks)
        )

        # Language model head
        self.ln = LayerNorm((embedding_dim,), dtype=dtype)
        self.lm_head = Linear(embedding_dim, n_embeddings, dtype=dtype)
        self.lm_head.w = self.token_emb.w  # weight sharing

        self.pos = Buffer(insert_dim(arange(max_seq_len, dtype=int32), 0))

    @Module.register_forward
    def forward(self, x: Tensor) -> Tensor:
        pos = self.pos[:, : x.shape[-1]]
        x = self.token_emb(x) + self.pos_emb(pos)
        for block in self.blocks:
            x = block(x)
        return self.lm_head(self.ln(x))

    @Module.register_backward
    def backward(self, dy: Tensor) -> None:
        dy = self.ln.backward(self.lm_head.backward(dy))
        for module in reversed(self.blocks):
            dy = module.backward(dy)
        self.token_emb.backward(dy)
        self.pos_emb.backward(dy.sum(axis=0))


class TransformerBlock(Module):
    """Decoder-only transformer block consisting of a multi head attention block
    and a feed forward block.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    ffwd_channels : int
        Number of channels of the hidden layer in the feed forward block.
    n_heads : int
        Number of attention heads.
    out_scale : float, optional
        Scale for the output projection. Defaults to ``1.0``.
    mask : Tensor, optional
        Attention-mask. Defaults to ``None``.
        Must be a zeros-tensor with values of ```-inf`` indicating elements to be masked out.
    dropout_p : float, optional
        Dropout probability. Defaults to ``0.2``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to ``None``.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.
    """

    def __init__(
        self,
        in_channels: int,
        ffwd_channels: int,
        n_heads: int,
        out_scale: float = 1.0,
        mask: Optional[Tensor] = None,
        dropout_p: float = 0.2,
        dtype: Optional[DType] = None,
        label: Optional[str] = None,
    ) -> None:
        super().__init__(label)

        self.ln_1 = LayerNorm((in_channels,), dtype=dtype)
        self.attn = MultiHeadAttention(
            in_channels, n_heads, mask, dropout_p, out_scale, dtype
        )
        self.dropout_1 = Dropout(dropout_p)

        self.ln_2 = LayerNorm((in_channels,), dtype=dtype)
        self.ffwd = FeedForward(in_channels, ffwd_channels, out_scale, dtype)
        self.dropout_2 = Dropout(dropout_p)

    @Module.register_forward
    def forward(self, x: Tensor) -> Tensor:
        x = x + self.dropout_1(self.attn(self.ln_1(x)))
        x = x + self.dropout_2(self.ffwd(self.ln_2(x)))
        return x

    @Module.register_backward
    def backward(self, dy: Tensor) -> Tensor:
        dy = dy + self.ln_2.backward(self.ffwd.backward(self.dropout_2.backward(dy)))
        dy = dy + self.ln_1.backward(self.attn.backward(self.dropout_1.backward(dy)))
        return dy


class FeedForward(Module):
    """FeedForward block for transformers.

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    h_channels : int
        Number of channels of the hidden layer.
    out_scale : float, optional
        Scale for the output projection. Defaults to ``1.0``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to ``None``.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.
    """

    def __init__(
        self,
        in_channels: int,
        h_channels: int,
        out_scale: float = 1.0,
        dtype: Optional[DType] = None,
        label: Optional[str] = None,
    ) -> None:
        super().__init__(label)
        self.up_proj = Linear(in_channels, h_channels, dtype=dtype)
        self.act = ReLU()
        self.down_proj = Linear(h_channels, in_channels, dtype=dtype)
        self.down_proj.w *= out_scale

    @Module.register_forward
    def forward(self, x: Tensor) -> Tensor:
        x = self.up_proj(x)
        x = self.act(x)
        x = self.down_proj(x)
        return x

    @Module.register_backward
    def backward(self, dy: Tensor) -> Tensor:
        dy = self.down_proj.backward(dy)
        dy = self.act.backward(dy)
        dy = self.up_proj.backward(dy)
        return dy


class MultiHeadAttention(Module):
    r"""Multi Head Self-Attention as described by
    `Vaswani et al., 2017 <https://arxiv.org/pdf/1706.03762>`_.

    .. math::
        \begin{array}{ll} \\
            Q = xW_Q^T \\
            K = xW_K^T \\
            V = xW_V^T \\
            \text{MultiHeadAttention}(x) = \text{concat}(\text{Attention}(Q_1, K_1, V_1), ..., \text{Attention}(Q_n, K_n, V_n))W_o^T \\
            \text{Attention}(Q, K, V) = \text{softmax}(\frac{QK^T}{\sqrt{N}}) \cdot V \\
        \end{array}

    where :math:`N` is the number of attention heads.

    Shapes:
        - Input :math:`(B, S, C_{in})`
        - Output :math:`(B, S, C_{in})`
    where
        - :math:`B` ... batch axis
        - :math:`S` ... sequence
        - :math:`C_{in}` ... input channels

    Parameters
    ----------
    in_channels : int
        Number of input channels.
    n_heads : int
        Number of attention heads.
    mask : Tensor, optional
        Attention-mask. Defaults to ``None``.
        Must be a zeros-tensor with values of ```-inf`` indicating elements to be masked out.
    dropout_p : float, optional
        Dropout probability. Defaults to ``0``.
    out_scale : float, optional
        Scale for the output projection. Defaults to ``1.0``.
    dtype: DtypeLike, optional
        Datatype of weights and biases. Defaults to ``None``.
    label: str, optional
        Module label. Defaults to ``None``. If `None`, the class name is used.


    .. note::
        All weights are initialized from :math:`\mathcal{U}(-k, k)`, where
        :math:`k = \sqrt{\frac{1}{C_{in}}}`. Biases are initialized as zeros.

    .. note::
        Input projections do not use bias.
    """

    def __init__(
        self,
        in_channels: int,
        n_heads: int,
        mask: Optional[Tensor] = None,
        dropout_p: float = 0,
        out_scale: float = 1.0,
        dtype: Optional[DType] = None,
        label: Optional[str] = None,
    ) -> None:
        if in_channels % n_heads != 0:
            raise ValueError("Number of input channels must be divisible by n_heads.")
        super().__init__(label)

        self.n_heads = n_heads
        self.mask = None if not mask else Buffer(mask)
        self.dropout_p = dropout_p
        self.attn_w: list[Optional[Tensor]] = []

        self.query_proj = Linear(in_channels, in_channels, False, dtype, "QueryProj")
        self.key_proj = Linear(in_channels, in_channels, False, dtype, "KeyProj")
        self.value_proj = Linear(in_channels, in_channels, False, dtype, "ValueProj")

        self.out_proj = Linear(in_channels, in_channels, dtype=dtype, label="OutProj")
        self.out_proj.w *= out_scale

    @Module.register_forward
    def forward(self, x: Tensor) -> Tensor:
        validate_input_axes(self, x, [3])
        dropout_p = self.dropout_p if self._is_training else 0
        y_heads = []

        # input projection for self-attention
        q = self.query_proj(x)
        k = self.key_proj(x)
        v = self.value_proj(x)

        # split projections for each head
        q_heads = split(q, self.n_heads)
        k_heads = split(k, self.n_heads)
        v_heads = split(v, self.n_heads)

        # multi head attention: compute attention weights for each head, concat results
        for q_head, k_head, v_head in zip(q_heads, k_heads, v_heads):
            y_head, attn_w_head = FSDPAttention.forward(
                self.fcache,
                q_head,
                k_head,
                v_head,
                self.mask,
                dropout_p,
                self._is_retaining_values,
            )
            y_heads.append(y_head)
            self.attn_w.append(attn_w_head)
        y = concat(y_heads)

        # output projection
        return self.out_proj(y)

    @Module.register_backward
    def backward(self, dy: Tensor) -> Tensor:
        dq_heads, dk_heads, dv_heads = [], [], []

        # output gradients
        dy = self.out_proj.backward(dy)

        # split output gradients for each head
        dy_heads = split(dy, self.n_heads)

        # multi head attention gradients
        for dy_head in reversed(dy_heads):  # reversed, because of cache order
            dq_head, dk_head, dv_head = FSDPAttention.backward(self.fcache, dy_head)
            dq_heads.append(dq_head)
            dk_heads.append(dk_head)
            dv_heads.append(dv_head)

        # merge heads
        dq = concat(dq_heads[::-1])
        dk = concat(dk_heads[::-1])
        dv = concat(dv_heads[::-1])

        # input projection gradients
        dx1 = self.query_proj.backward(dq)
        dx2 = self.key_proj.backward(dk)
        dx3 = self.value_proj.backward(dv)

        return dx1 + dx2 + dx3


class FSDPAttention(Function):
    """Computes the scaled dot product attention scores."""

    @staticmethod
    def forward(
        cache: FunctionCache,
        q: Tensor,
        k: Tensor,
        v: Tensor,
        mask: Optional[Tensor],
        dropout_p: float,
        return_attn_w: bool,
    ) -> tuple[Tensor, Optional[Tensor]]:
        _, seq_len, head_size = q.shape

        attn_w = q @ k.T / math.sqrt(head_size)
        if mask is not None:
            attn_w += mask[:seq_len, :seq_len]
        attn_w = FSoftmax.forward(cache, attn_w)
        attn_w = FDropout.forward(cache, attn_w, dropout_p, dropout_p > 0)
        y = attn_w @ v

        cache.q, cache.k, cache.v, cache.attn_w = q, k, v, attn_w
        return y, (None if not return_attn_w else attn_w)

    @staticmethod
    def backward(cache: FunctionCache, dy: Tensor) -> tuple[Tensor, Tensor, Tensor]:
        q, k, v, attn_w = cache.q, cache.k, cache.v, cache.attn_w
        head_size = q.shape[-1]

        # attention gradients
        dattn_w = dy @ v.T
        dattn_w = FDropout.backward(cache, dattn_w)
        dattn_w = FSoftmax.backward(cache, dattn_w) / math.sqrt(head_size)

        # query, key, value gradients
        dq = dattn_w @ k
        dk = dattn_w.T @ q
        dv = attn_w.T @ dy

        return dq, dk, dv


def sdp_attention(
    q: Tensor,
    k: Tensor,
    v: Tensor,
    mask: Optional[Tensor] = None,
    dropout_p: float = 0,
    return_attn_w: bool = False,
) -> tuple[Tensor, Optional[Tensor]]:
    r"""Computes the scaled dot product attention scores.

    Parameters
    ----------
    q : Tensor
        Query tensor.
    k : Tensor
        Key tensor.
    v : Tensor
        Value tensor.
    mask : Tensor, optional
        Attention-mask. Defaults to ``None``.
        Must be a zeros-tensor with values of ```-inf`` indicating elements to be masked out.
    dropout_p : float, optional
        Dropout probability of attention weights. Defaults to ``0``.
    return_attn_w : bool, optional
        Whether to also return the computed attention weights. Defaults to ``False``.
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
    return FSDPAttention.forward(PseudoCache(), q, k, v, mask, dropout_p, return_attn_w)


class PositionalEncoding(Module):
    r"""Sinusoidal Positional Encoding layer as described by
    `Vaswani et al., 2017 <https://arxiv.org/pdf/1706.03762>`_.

    .. math::
        \begin{array}{ll} \\
            PE_{(pos, 2i)} = \text{sin}(pos \cdot e^{-2i \frac{log(b)}{E})
            PE_{(pos, 2i+1)} = \text{cos}(pos \cdot e^{-2i \frac{log(b)}{E})
        \end{array}

    where :math:`E` is the embedding dimension and :math:`b` is the base.

    Shapes:
        - Input :math:`(B_1, ... , B_n, S)`
        - Output :math:`(B_1, ... , B_n, S, E)`
    where
        - :math:`B_1, ... , B_n` ... batch axes
        - :math:`S` ... sequence
        - :math:`E` ... embedding dimension

    Parameters
    ----------
    max_seq_len : int
        Maximum possible length of the input sequence.
    embedding_dim : int
        Embedding vector dimensions.
    base : float, optional
        Base for computing the positional encoding. Defaults to ``1e4``.
    dtype : DType, optional
        Datatype of weights and biases. Defaults to ``None``.
    label : str, optional
        Module label. Defaults to ``None``. If ``None``, the class name is used.
    """

    def __init__(
        self,
        max_seq_len: int,
        embedding_dim: int,
        base: float = 1e4,
        dtype: Optional[DType] = None,
        label: Optional[str] = None,
    ) -> None:
        super().__init__(label)
        self.max_seq_len = max_seq_len
        self.embedding_dim = embedding_dim

        # compute positional encodings
        encodings = zeros((max_seq_len, embedding_dim), dtype=dtype)
        positions = insert_dim(arange(max_seq_len, dtype=dtype), -1)
        emb_range = arange(embedding_dim, step=2, dtype=dtype)
        div_term = exp(emb_range * (-(math.log(base) / embedding_dim)))
        encodings[:, 0::2] = sin(positions * div_term)
        encodings[:, 1::2] = cos(positions * div_term)

        self.encodings = Buffer(encodings)

    @Module.register_forward
    def forward(self, x: Tensor) -> Tensor:
        return self.encodings[: x.shape[1]]

    @Module.register_backward
    def backward(self, dy: Tensor) -> Tensor:
        return dy
