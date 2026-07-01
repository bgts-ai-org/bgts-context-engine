"""Determinism + shape tests for the embedding encoder (P2)."""

from __future__ import annotations

import math

from cce.indexing.embedder.encoder import HashingEncoder, build_default_encoder


def test_encoder_is_deterministic():
    enc = HashingEncoder(dim=64)
    a = enc.encode("def get_user(id): return id")
    b = enc.encode("def get_user(id): return id")
    assert a == b


def test_encoder_dimension_and_unit_norm():
    enc = HashingEncoder(dim=128)
    vec = enc.encode("class Service handles authentication")
    assert len(vec) == 128
    norm = math.sqrt(sum(v * v for v in vec))
    assert abs(norm - 1.0) < 1e-6


def test_encoder_empty_text_is_zero_vector():
    enc = HashingEncoder(dim=32)
    assert enc.encode("") == [0.0] * 32


def test_similar_text_closer_than_unrelated():
    enc = HashingEncoder(dim=256)

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=False))

    q = enc.encode("authenticate user login token")
    near = enc.encode("user authentication login")
    far = enc.encode("matrix multiplication numeric solver")
    assert cos(q, near) > cos(q, far)


def test_default_encoder_pins_model_id():
    enc = build_default_encoder(dim=64, model_id="unset")
    assert enc.model_id == "cce-hashing-v1"
    enc2 = build_default_encoder(dim=64, model_id="my-pinned-model")
    assert enc2.model_id == "my-pinned-model"
