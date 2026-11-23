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


def test_not_a_pdf_is_rejected():
    with pytest.raises(PdfRejected):
        parse_pdf(b"MZ\x90\x00 isto nao e pdf")
    with pytest.raises(PdfRejected):
        parse_pdf(b"%PDF-1.4\n garbage")


@pytest.mark.parametrize("kw", [
    {"catalog_extra": b"/OpenAction << /S /JavaScript /JS (app.alert(1)) >>"},
    {"catalog_extra": b"/AA << /WC << /S /JavaScript /JS (x) >> >>"},
    {"catalog_extra": b"/Names << /JavaScript 6 0 R >>", "extra_objects": (b"<< /Names [] >>",)},
    {"page_extra": b"/Annots [ << /Type /Annot /Subtype /Link /Rect [0 0 9 9] /A << /S /Launch /F (cmd.exe) >> >> ]"},
    {"page_extra": b"/AA << /O << /S /JavaScript /JS (x) >> >>"},
    {"extra_objects": (b"<< /Type /EmbeddedFile /Length 0 >>\nstream\n\nendstream",)},
    {"extra_objects": (b"<< /S /JavaScript /JS (this.print()) >>",)},  # objeto solto no arquivo
])
def test_active_content_is_rejected(kw):
    with pytest.raises(PdfRejected):
        parse_pdf(make_pdf(danfe_text(), **kw))


def test_random_js_bytes_inside_compressed_data_are_not_malicious():
    """Regressão: um PDF real foi recusado porque os bytes '/JS' apareciam por acaso num bloco comprimido."""
    blob = b"<< /Length 24 >>\nstream\n\x88>\xd4^/JS\xd6\x98 /JavaScript? /Launch\nendstream"
    p = parse_pdf(make_pdf(danfe_text(), extra_objects=(blob,)))
    assert p.numero == "316"
    p = parse_pdf(danfe_pdf() + b"\n% /JavaScript (isto e so um comentario)\n")
    assert p.numero == "316"


def test_bank_data_in_service_description_is_cut():
    from app.parsing import cut_sensitive
    assert cut_sensitive("Carro de som Dados bancários: Caixa Agência 4807 Conta 577 Pix 7399") == "Carro de som"
    assert cut_sensitive("Publicidade. Chave Pix: x@y.com") == "Publicidade"
    assert cut_sensitive("Banner lona 3x2") == "Banner lona 3x2"
    assert cut_sensitive("Dados bancários: tudo") == ""


@pytest.mark.parametrize("raw,cents", [("1.300,00", 130000), ("0,05", 5), ("12.345.678,90", 1234567890)])
def test_parse_money(raw, cents):
    assert parse_money(raw) == cents


@pytest.mark.parametrize("raw,expected", [
    # padrões vistos em notas reais (números trocados por fictícios)
    ("Panfletagem em Porto Seguro. CONTA PARA CRÉDITO Banco: NuBank Agência: 0001", "Panfletagem em Porto Seguro"),
    ("Outdoor 30 dias. Valor líquido: R$ 1.250,00 Dados para pagamento: Ag: 0001 Conta: 11111111-1",
     "Outdoor 30 dias. Valor líquido: R$ 1.250,00"),
    ("INSERÇÃO DE MATERIAL PUBLICITÁRIO PAGAMENTO VIA BOLETO BANCÁRIO OU DEPÓSITO NA CONTA: AG: 0001",
     "INSERÇÃO DE MATERIAL PUBLICITÁRIO"),
    ("EVENTO: INAUGURAÇÃO BOLETO BANCÁRIODADOS BANCÁRIOS: CAIXA ECONÔMICA AG 9999", "EVENTO: INAUGURAÇÃO BOLETO BANCÁRIO"),
    ("Decoração em balões. loja Porto Seguro obs: Dados para o pagamneto", "Decoração em balões. loja Porto Seguro"),
    ("TENDA 5x5. DADOS PARA PAGAMENTO CNPJ - PIX: 00000000000000 FAVORECIDO: X C/C 99999- 6", "TENDA 5x5"),
    ("Mídia no aeroporto. VENCIMENTO: 22/06/2026 Dados para deposito", "Mídia no aeroporto. VENCIMENTO: 22/06/2026"),
    ("Captação de imagens. PIX:00.000.000/0001-00 FULANO.Banco: Mercado Pago", "Captação de imagens"),
    ("Montagem de evento Dados para pagamento Empresa X CPF 000.000.000-00 Banco Y", "Montagem de evento"),
    ("VALOR ENTRADA SERVIÇOS DE COMUNICAÇÃO VISUAL. BANCO ITAU AGENCI 9999 C/C 99999-", "VALOR ENTRADA SERVIÇOS DE COMUNICAÇÃO VISUAL"),
    # não corta o que é só serviço
    ("Veiculação em agosto 2026, 54 inserções", "Veiculação em agosto 2026, 54 inserções"),
    ("Impressão. Valor aprox. trib. Lei 12.741-2012: R$ 386,40", "Impressão. Valor aprox. trib. Lei 12.741-2012: R$ 386,40"),
    ("Agenciamento de mídia regional", "Agenciamento de mídia regional"),
])
def test_cut_sensitive_real_world_patterns(raw, expected):
    from app.parsing import cut_sensitive
    assert cut_sensitive(raw) == expected
