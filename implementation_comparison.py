import sys
import time

import compyute as cp
import requests
from compyute import nn
from tokenizers.character_tokenizer import CharacterTokenizer

from transformer.experimental.gpt_debug import GPTTransformer
from transformer.attention_funcs import get_causal_mask


def main() -> None:
    cp.random.set_seed(1337)
    device = cp.cuda

    # hyperparameters
    embed_dims = 384
    context_length = 256
    n_heads = 6
    n_blocks = 6
    batch_size = 64
    val_interval = 250

    # load data
    DATA_URL = "https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt"
    response = requests.get(DATA_URL)
    data = response.text

    # tokenize data
    chars = sorted(list(set(data)))
    tokenizer = CharacterTokenizer()
    tokenizer.vocab = {i: c for i, c in enumerate(chars)}
    tokenizer.ivocab = {c: i for i, c in enumerate(chars)}
    data_enc = tokenizer.encode(data)

    # prepare data
    data_enc_t = cp.tensor(data_enc, dtype=cp.int32)
    X_train = cp.stack(
        [
            data_enc_t[i * context_length : i * context_length + context_length]
            for i in range(len(data_enc_t) // context_length)
        ]
    )
    y_train = cp.stack(
        [
            data_enc_t[i * context_length + 1 : i * context_length + context_length + 1]
            for i in range(len(data_enc_t) // context_length)
        ]
    )

    # create model
    mask = get_causal_mask(context_length)

    if len(sys.argv) != 2:
        raise ValueError("Must provide implementation as argument.")
    implementation = sys.argv[1]

    if implementation not in {"batched", "unbatched", "semibatched"}:
        raise ValueError(
            "Invalid implementation argument. Must be one of 'batched', 'unbatched', 'semibatched'."
        )
    print(f"Using {implementation} attenttion implementation.")

    model = GPTTransformer(
        n_embeds=tokenizer.vocab_size,
        embed_dim=embed_dims,
        mlp_channels=4 * embed_dims,
        n_heads=n_heads,
        n_blocks=n_blocks,
        max_context_len=context_length,
        mask=mask,
        implementation=implementation,
    )
    model.to_device(device)

    # training
    train_dl = nn.utils.Dataloader((X_train, y_train), batch_size, device)
    loss_fn = nn.CrossEntropyLoss()
    optim = nn.optimizers.AdamW(model.get_parameters(), lr=3e-4)

    step = 1
    for x, y in train_dl():
        start = time.perf_counter()

        model.training()
        loss = loss_fn(model(x), y).item()
        model.backward(loss_fn.backward())

        optim.step()
        optim.reset_grads()

        cp.backend.synchronize()
        dt = time.perf_counter() - start

        tok_per_s = batch_size * context_length / dt
        print(
            f"step {step:4} | loss {loss:.4f} | dt {dt:.4f} s | {tok_per_s:.1f} tokens/s"
        )
        step += 1


if __name__ == "__main__":
    main()