import pytest

from app.codes import GS, classify, gtin_check_ok, gtin_from_datamatrix

MIG = "05909990657971"           # GTIN-14 opakowania MIG 400 (EAN-13 na pudełku: 5909990657971)


def test_check_digit():
    assert gtin_check_ok(MIG) and gtin_check_ok(MIG[1:])
    assert not gtin_check_ok("5909990657972")


@pytest.mark.parametrize("raw", [
    "5909990657971",                 # EAN-13 z kodu kreskowego
    "05909990657971",                # GTIN-14
    "@5909990657971",                # z prefiksem
    "@ 05909990657971 ",
    "590-9990-657971",
])
def test_plain_gtin(raw):
    q = classify(raw)
    assert (q.kind, q.value, q.source, q.valid) == ("gtin", MIG, "gtin", True)


@pytest.mark.parametrize("raw", [
    f"01{MIG}21ABC123XYZ{GS}17281231{GS}10LOT42",              # skaner z separatorami GS
    f"]d201{MIG}17281231{GS}10LOT42{GS}21SN0001",              # z prefiksem symbologii
    f"01{MIG}1728123110LOT4221SN0001",                         # separatory zgubione po drodze
    f"(01){MIG}(17)281231(10)LOT42(21)SN0001",                 # zapis czytelny z nawiasami
    f"@01{MIG}21ABC{GS}17281231",                              # z prefiksem użytkownika
    f"21SN0001{GS}01{MIG}{GS}17281231",                        # (01) nie na początku
])
def test_datamatrix(raw):
    q = classify(raw)
    assert (q.kind, q.value, q.source) == ("gtin", MIG, "datamatrix")


def test_custom_prefix():
    assert classify("#5909990657971", prefixes="@#").value == MIG
    assert classify("#5909990657971", prefixes="@").kind == "name"


def test_invalid_check_digit_is_still_a_code():
    q = classify("5909990657972")
    assert q.kind == "gtin" and not q.valid


@pytest.mark.parametrize("raw", ["Ibuprom Max", "MIG 400", "0,9% Sodium Chloride-BRAUN", "1% Wodny roztwór", "Vitaminum B12"])
def test_names(raw):
    assert classify(raw).kind == "name"


def test_empty():
    assert classify("  @ ").kind == "empty"
    assert gtin_from_datamatrix("10LOT42") is None
