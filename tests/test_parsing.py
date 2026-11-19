import pytest

from app.parsing import (
    ParseError,
    PdfRejected,
    nfe_key_is_valid,
    parse_money,
    parse_pdf,
)
from tests.helpers import danfe_pdf, danfe_text, make_pdf, nfe_key, nfse_pdf


def test_danfe_fields():
    p = parse_pdf(danfe_pdf())
    assert (p.kind, p.numero, p.serie) == ("danfe", "316", "1")
    assert p.emit_nome == "GRAFICA EXEMPLO LTDA" and p.emit_doc == "11.222.333/0001-81"
    assert p.tom_nome == "EMPRESA TOMADORA S/A" and p.tom_doc == "11.444.777/0001-61"
    assert p.valor_cents == 140000 and p.desconto_cents == 10000
    assert str(p.data_emissao) == "2026-03-04" and str(p.vencimento) == "2026-03-31"
    assert p.descricao == "LONA IMPRESSA COM ILHOS"
    assert len(p.chave) == 44


def test_nfse_fields():
    p = parse_pdf(nfse_pdf(numero=12, total="2.345,67"))
    assert (p.kind, p.numero) == ("nfse", "12")
    assert p.valor_cents == 234567 and str(p.data_emissao) == "2026-05-28"
    assert p.emit_nome == "FULANA DE TAL ME"  # o CPF colado ao nome do MEI é removido
    assert p.tom_nome == "EMPRESA TOMADORA S/A"
    assert "publicidade" in p.descricao and len(p.chave) == 50


def test_real_world_danfe_key_is_valid():
    assert nfe_key_is_valid("29260337863976000106550010000003161082938810")
    assert not nfe_key_is_valid("29260337863976000106550010000003161082938811")


def test_danfe_with_wrong_check_digit_is_rejected():
    good = nfe_key(numero=5)
    bad = good[:-1] + str((int(good[-1]) + 1) % 10)
    text = danfe_text(numero=5).replace(" ".join(good[i:i + 4] for i in range(0, 44, 4)),
                                         " ".join(bad[i:i + 4] for i in range(0, 44, 4)))
    with pytest.raises(ParseError, match="verificador"):
        parse_pdf(make_pdf(text))


def test_danfe_number_must_match_key():
    text = danfe_text(numero=5)
    with pytest.raises(ParseError):  # chave é do nº 5, mas o corpo diz outro valor de total zerado
        parse_pdf(make_pdf(text.replace("1.400,00", "0,00")))


def test_pdf_that_is_not_a_nota():
    with pytest.raises(ParseError, match="não parece"):
        parse_pdf(make_pdf("Relatório qualquer\n" * 20 + "x" * 200))


def test_not_a_pdf_and_scripts_are_rejected():
    with pytest.raises(PdfRejected):
        parse_pdf(b"MZ\x90\x00 isto nao e pdf")
    with pytest.raises(PdfRejected, match="script"):
        parse_pdf(danfe_pdf() + b"\n% /JavaScript (app.alert(1))")
    with pytest.raises(PdfRejected):
        parse_pdf(b"%PDF-1.4\n garbage")


@pytest.mark.parametrize("raw,cents", [("1.300,00", 130000), ("0,05", 5), ("12.345.678,90", 1234567890)])
def test_parse_money(raw, cents):
    assert parse_money(raw) == cents
