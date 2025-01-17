from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock

import pytest
import torch
from lightning import Fabric


def test_lora_layer_replacement():
    from lit_gpt.lora import CausalSelfAttention as LoRACausalSelfAttention, GPT, Config, LoRALinear

    config = Config(n_layer=2, n_head=4, n_embd=8, block_size=8, vocab_size=8, r=8, alpha=8, dropout=0.1)
    model = GPT(config)

    assert isinstance(model.transformer.h[0].attn, LoRACausalSelfAttention)
    assert isinstance(model.transformer.h[1].attn, LoRACausalSelfAttention)
    assert isinstance(model.lm_head, LoRALinear)
    assert isinstance(model.transformer.h[0].mlp.proj, LoRALinear)


def test_lora_merge():
    from lit_gpt.lora import mark_only_lora_as_trainable, merge_lora_weights, GPT, Config

    config = Config(
        n_layer=1,
        n_head=2,
        n_embd=8,
        block_size=8,
        vocab_size=8,
        r=8,
        alpha=8,
        dropout=0.1,
        to_query=True,
        to_value=True,
        to_projection=True,
    )
    model = GPT(config)
    model.train()

    initial_weight = model.transformer.h[0].attn.proj.linear.weight.clone()
    assert torch.equal(model.transformer.h[0].attn.proj.linear.weight, initial_weight)

    # perform an update to the LoRA weights
    mark_only_lora_as_trainable(model)
    optimizer = torch.optim.SGD(model.parameters(), lr=1.0)
    y = model(torch.randint(0, 8, size=(2, 4), dtype=torch.int64))
    y.sum().backward()
    optimizer.step()
    optimizer.zero_grad()
    # the weight remains unchanged (only lora A and B change)
    assert torch.equal(model.transformer.h[0].attn.proj.linear.weight, initial_weight)

    # calling merge() multiple times in a row should not merge multiple times
    merge_lora_weights(model)
    assert model.transformer.h[0].attn.attn.merged
    weight_after = model.transformer.h[0].attn.proj.linear.weight.clone()
    merge_lora_weights(model)
    merge_lora_weights(model)
    assert torch.equal(model.transformer.h[0].attn.proj.linear.weight, weight_after)

    # check that `W_after = W_initial + (A x B)`
    a = model.transformer.h[0].attn.proj.lora_A
    b = model.transformer.h[0].attn.proj.lora_B
    scaling = model.transformer.h[0].attn.proj.scaling
    delta_w = (b @ a) * scaling
    torch.testing.assert_close(weight_after, initial_weight + delta_w)


def test_lora_mqa_gqa():
    from lit_gpt.lora import GPT, Config

    # MHA
    config = Config(
        n_layer=1,
        n_head=4,
        n_embd=8,
        block_size=1,
        vocab_size=1,
        r=2,
        alpha=8,
        dropout=0.1,
        to_query=True,
        to_value=True,
    )
    assert config.n_query_groups == config.n_head
    model = GPT(config)
    attn = model.transformer.h[0].attn.attn
    assert attn.linear.weight.shape == (24, 8)
    assert attn.lora_A.shape == (4, 8)
    assert attn.lora_B.shape == (16, 2)
    assert attn.lora_ind == [0, 1, 2, 3, 4, 5, 6, 7, 16, 17, 18, 19, 20, 21, 22, 23]
    x = torch.randint(0, 8, size=(3, 5, 16), dtype=torch.int64)
    assert attn.zero_pad(x).shape == (3, 5, 24)

    # MQA
    config.n_query_groups = 1
    model = GPT(config)
    attn = model.transformer.h[0].attn.attn
    assert attn.linear.weight.shape == (12, 8)
    assert attn.lora_A.shape == (4, 8)
    assert attn.lora_B.shape == (10, 2)
    assert attn.lora_ind == [0, 1, 2, 3, 4, 5, 6, 7, 10, 11]
    x = torch.randint(0, 8, size=(3, 5, 10), dtype=torch.int64)
    assert attn.zero_pad(x).shape == (3, 5, 12)

    # GQA
    config.n_query_groups = 2
    model = GPT(config)
    attn = model.transformer.h[0].attn.attn
    assert attn.linear.weight.shape == (16, 8)
    assert attn.lora_A.shape == (4, 8)
    assert attn.lora_B.shape == (12, 2)
    assert attn.lora_ind == [0, 1, 2, 3, 4, 5, 6, 7, 12, 13, 14, 15]
    x = torch.randint(0, 8, size=(3, 5, 12), dtype=torch.int64)
    assert attn.zero_pad(x).shape == (3, 5, 16)


def test_lora_filter(tmp_path):
    from lit_gpt.lora import lora_filter, GPT

    fabric = Fabric(devices=1)
    model = GPT.from_name("pythia-70m", n_layer=3, r=1, to_query=True, to_value=True)
    save_path = tmp_path / "model.pth"
    fabric.save(save_path, {"model": model}, filter={"model": lora_filter})
    saved = torch.load(save_path)["model"]

    expected = {
        "transformer.h.1.attn.attn.lora_B",
        "transformer.h.2.attn.attn.lora_B",
        "transformer.h.2.attn.attn.lora_A",
        "transformer.h.1.attn.attn.lora_A",
        "transformer.h.0.attn.attn.lora_A",
        "transformer.h.0.attn.attn.lora_B",
    }
    assert set(saved) == expected


def test_lora_script(tmp_path, fake_checkpoint_dir, monkeypatch):
    import finetune.lora as module

    module.gradient_accumulation_iters = 1
    module.save_interval = 2
    module.eval_interval = 2
    module.eval_iters = 2
    module.max_iters = 6

    data = [
        {"input_ids": torch.tensor([0, 1, 2]), "labels": torch.tensor([1, 2, 3])},
        {"input_ids": torch.tensor([1, 2, 3]), "labels": torch.tensor([2, 3, 4])},
    ]
    torch.save(data, tmp_path / "train.pt")
    torch.save(data, tmp_path / "test.pt")

    from lit_gpt.config import name_to_config

    model_config = dict(block_size=128, n_layer=2, n_embd=8, n_head=4, padded_vocab_size=8)
    monkeypatch.setitem(name_to_config, "tmp", model_config)

    load_mock = Mock()
    load_mock.return_value = load_mock
    load_mock.__enter__ = Mock()
    load_mock.__exit__ = Mock()
    monkeypatch.setattr(module, "lazy_load", load_mock)

    tokenizer_mock = Mock()
    tokenizer_mock.return_value = tokenizer_mock
    tokenizer_mock.encode = lambda *_, **kwargs: torch.tensor([3, 2, 1], **kwargs)
    monkeypatch.setattr(module, "Tokenizer", tokenizer_mock)

    stdout = StringIO()
    with redirect_stdout(stdout):
        module.setup(data_dir=tmp_path, checkpoint_dir=fake_checkpoint_dir, out_dir=tmp_path, precision="32-true")

    assert {p.name for p in tmp_path.glob("*.pth")} == {
        "iter-000001-ckpt.pth",
        "iter-000003-ckpt.pth",
        "iter-000005-ckpt.pth",
        "lit_model_lora_finetuned.pth",
    }
    assert (tmp_path / "version_0" / "metrics.csv").is_file()

    logs = stdout.getvalue()
    assert logs.count("optimizer.step") == module.max_iters
    assert logs.count("val loss") == module.max_iters // module.eval_interval
    assert "of trainable parameters: 512" in logs


def test_lora_init_when_linear_overridden():
    from lit_gpt.lora import LoRAQKVLinear

    class MyLinear(torch.nn.Linear):
        def __init__(self, *args, **kwargs):
            # this needs to be implemented to demonstrate the failure
            super().__init__(*args, **kwargs)

    original_linear = torch.nn.Linear
    # Our bnb does this sort of monkey patching
    torch.nn.Linear = MyLinear
    layer = LoRAQKVLinear(1, 1, 1, 1)
    assert isinstance(layer.linear, original_linear)
    torch.nn.Linear = original_linear


@pytest.mark.parametrize(
    ("apply_to", "target_layer_names", "mlp_class_name"),
    (
        ("to_projection", "transformer.h.0.attn.proj", "GptNeoxMLP"),
        ("to_mlp", {"transformer.h.0.mlp.fc", "transformer.h.0.mlp.proj"}, "GptNeoxMLP"),
        ("to_head", "lm_head", "GptNeoxMLP"),
        ("to_projection", "transformer.h.0.attn.proj", "LLaMAMLP"),
        ("to_mlp", {"transformer.h.0.mlp.fc_1", "transformer.h.0.mlp.fc_2", "transformer.h.0.mlp.proj"}, "LLaMAMLP"),
        ("to_head", "lm_head", "LLaMAMLP"),
    ),
)
def test_lora_linear_utilization(apply_to, target_layer_names, mlp_class_name):
    from lit_gpt.lora import GPT, Config

    config = Config(
        n_layer=1,
        n_head=4,
        n_embd=8,
        block_size=1,
        vocab_size=1,
        r=2,
        alpha=8,
        dropout=0.1,
        _mlp_class=mlp_class_name,
        intermediate_size=8 * 3,
        **{apply_to: True}
    )
    model = GPT(config)
    state_dict = model.state_dict()

    if isinstance(target_layer_names, str):
        target_layer_names = {target_layer_names}
    lora_sublayers = (".lora_A", ".lora_B")

    # check that all the target layers have LoRA weights
    for layer_name in target_layer_names:
        for lora_sublayer in lora_sublayers:
            assert layer_name + lora_sublayer in state_dict

    # check that only target layers have LoRA weights
    lora_params = [k for k in state_dict if k.endswith(lora_sublayers)]
    lora_params = {k[:-7] for k in lora_params}
    assert lora_params == target_layer_names


@pytest.mark.parametrize("apply_to", (None, "to_query", "to_key", "to_value", "to_projection", "to_mlp", "to_head"))
def test_lora_layer_forward_no_exception(apply_to):
    from lit_gpt.lora import GPT, Config

    config = Config(n_layer=1, n_head=4, n_embd=8, block_size=1, vocab_size=1, r=2, alpha=8, dropout=0.1)
    if apply_to:
        setattr(config, apply_to, True)
    input_ids = torch.tensor([[1]])
    model = GPT(config)
    model.eval()

    model(input_ids)


@pytest.mark.parametrize(("rank", "expected_merged"), ((-1, False), (0, False), (1, True)))
def test_lora_linear_weights_merged_status(rank, expected_merged):
    from lit_gpt.lora import LoRALinear

    layer = LoRALinear(10, 10, r=rank)
    assert not layer.merged
    layer.merge()
    assert layer.merged == expected_merged


@pytest.mark.parametrize(
    ("rank", "enable_lora", "expected_merged"),
    ((-1, True, False), (0, True, False), (1, True, True), (-1, False, False), (0, False, False), (1, False, False)),
)
def test_lora_qkv_linear_weights_merged_status(rank, enable_lora, expected_merged):
    from lit_gpt.lora import LoRAQKVLinear

    layer = LoRAQKVLinear(10, 3 * 10, n_head=2, n_query_groups=2, r=rank, enable_lora=enable_lora)
    assert not layer.merged
    layer.merge()
    assert layer.merged == expected_merged


@pytest.mark.parametrize(
    ("mode", "expected"),
    (
        ("bnb.nf4", "Linear4bit"),
        ("bnb.nf4-dq", "Linear4bit"),
        ("bnb.fp4", "Linear4bit"),
        ("bnb.fp4-dq", "Linear4bit"),
        pytest.param(
            "bnb.int8",
            "Linear8bitLt",
            marks=[
                pytest.mark.skipif(not torch.cuda.is_available(), reason="8bit requires CUDA"),
                # platform dependent cuda issue: libbitsandbytes_cpu.so: undefined symbol: cget_col_row_stats
                pytest.mark.xfail(raises=AttributeError, strict=False),
            ],
        ),
    ),
)
def test_bnb_replacement(mode, expected):
    from quantize.bnb import _BITSANDBYTES_AVAILABLE

    if not _BITSANDBYTES_AVAILABLE:
        pytest.skip("BNB not available")

    from quantize.bnb import bnb
    from lit_gpt.utils import quantization
    from lit_gpt.lora import LoRAQKVLinear, LoRALinear

    with quantization(mode):
        linear = LoRALinear(1, 1)
        qkv = LoRAQKVLinear(1, 1, 1, 1)
    expected = getattr(bnb.modules, expected)
    assert isinstance(linear.linear, expected)
    assert isinstance(qkv.linear, expected)
