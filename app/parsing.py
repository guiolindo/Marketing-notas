"""Leitura do PDF de uma NFS-e (DANFSe nacional) ou de um DANFE (NF-e modelo 55).

Só lê o texto do PDF (sem OCR, sem serviço externo). Tudo que não bate é rejeitado: é melhor recusar um
arquivo do que gravar um valor errado.
"""
from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from pypdf import PdfReader
from pypdf.errors import PyPdfError

logging.getLogger("pypdf").setLevel(logging.ERROR)

MAX_PAGES = 10


class PdfRejected(Exception):
    """O arquivo não é um PDF aceitável (corrompido, protegido, com script...)."""


class ParseError(Exception):
    """O PDF é válido, mas não é uma NFS-e/DANFE que o sistema entenda."""


@dataclass
class ParsedNote:
    kind: str  # "nfse" | "danfe"
    chave: str
    numero: str
    serie: str
    emit_nome: str
    emit_doc: str
    tom_nome: str
    tom_doc: str
    data_emissao: date
    vencimento: date | None
    descricao: str
    valor_cents: int
    desconto_cents: int
    pages: int


# ---------------------------------------------------------------- PDF seguro

_RISKY_ACTIONS = {"/JavaScript", "/Launch", "/ImportData", "/SubmitForm", "/GoToR", "/GoToE", "/Rendition"}
_RISKY_KEYS = {"/JS", "/JavaScript", "/RichMedia", "/XFA", "/EmbeddedFiles", "/EmbeddedFile", "/OpenAction", "/AA"}
_MAX_OBJECTS = 200_000


def _resolve(obj):
    try:
        return obj.get_object()
    except AttributeError:
        return obj


def _walk_direct(obj, depth: int = 0) -> bool:
    """True se o objeto (e o que está aninhado nele, sem seguir referências) traz algo ativo."""
    if depth > 12:
        return True  # aninhamento absurdo: tratado como suspeito
    if isinstance(obj, dict):
        if _RISKY_KEYS & set(map(str, obj.keys())):
            return True
        if str(obj.get("/S", "")) in _RISKY_ACTIONS or str(obj.get("/Type", "")) == "/EmbeddedFile":
            return True
        return any(_walk_direct(v, depth + 1) for v in obj.values() if not hasattr(v, "idnum"))
    if isinstance(obj, list):
        return any(_walk_direct(v, depth + 1) for v in obj if not hasattr(v, "idnum"))
    return False


def _has_active_content(reader: PdfReader) -> bool:
    """Percorre os objetos de verdade (inclusive os de dentro de blocos comprimidos). Procurar bytes soltos como
    "/JS" dá falso positivo: esses 3 bytes aparecem por acaso em qualquer dado comprimido."""
    size = int(reader.trailer.get("/Size", 0))
    if size > _MAX_OBJECTS:
        raise PdfRejected("O PDF tem objetos demais.")
    for num in range(1, size):
        try:
            obj = reader.get_object(num)
        except Exception:  # noqa: BLE001 - objeto ilegível: ver abaixo
            continue
        if obj is not None and _walk_direct(obj):
            return True
    return _walk_direct(reader.trailer.get("/Root", {}))


def open_pdf(data: bytes) -> PdfReader:
    """Abre o PDF e recusa o que não deveria entrar: protegido, grande demais, com script ou anexo."""
    if not data.startswith(b"%PDF-") and b"%PDF-" not in data[:1024]:
        raise PdfRejected("O arquivo não é um PDF.")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise PdfRejected("O PDF está protegido por senha.")
        pages = len(reader.pages)
    except PdfRejected:
        raise
    except (PyPdfError, ValueError, KeyError, RecursionError, OSError, TypeError, AttributeError) as exc:
        raise PdfRejected("Não foi possível abrir o PDF (arquivo corrompido?).") from exc
    if pages < 1:
        raise PdfRejected("O PDF não tem páginas.")
    if pages > MAX_PAGES:
        raise PdfRejected(f"O PDF tem {pages} páginas; uma nota fiscal tem no máximo {MAX_PAGES}.")
    try:
        if _has_active_content(reader):
            raise PdfRejected("O PDF contém script, anexo ou ação automática e foi recusado.")
    except PdfRejected:
        raise
    except (PyPdfError, ValueError, KeyError, RecursionError, TypeError, AttributeError) as exc:
        raise PdfRejected("Não foi possível verificar a estrutura do PDF.") from exc
    return reader


def extract_text(reader: PdfReader, layout: bool = True) -> str:
    parts = []
    for page in reader.pages:
        try:
            parts.append((page.extract_text(extraction_mode="layout") if layout else page.extract_text()) or "")
        except (PyPdfError, ValueError, KeyError, RecursionError, TypeError, AttributeError):
            parts.append("")
    return "\n".join(parts)


# ---------------------------------------------------------------- utilidades

_MONEY = r"\d{1,3}(?:\.\d{3})*,\d{2}"
_CNPJ = r"\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}"
_CPF = r"\d{3}\.\d{3}\.\d{3}-\d{2}"
_DATE = r"\d{2}/\d{2}/\d{4}"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean(text: str, limit: int = 300) -> str:
    return _CTRL.sub(" ", re.sub(r"\s+", " ", text or "")).strip()[:limit]


# Onde começam os dados de pagamento que prestadores escrevem na descrição do serviço (vistos em notas reais).
# Sem \b no início de propósito: o texto do PDF às vezes vem colado ("BOLETO BANCÁRIODADOS BANCÁRIOS").
_SENSITIVE = re.compile(
    r"dados\s*(?:banc[aá]rios?|para\s+(?:o\s+)?(?:pagam\w*|dep[oó]sito|cr[eé]dito)|de\s+pagamento)"
    r"|conta\s+(?:para|de)\s+cr[eé]dito"
    r"|pagamento\s+via\s+boleto|dep[oó]sito\s+(?:na|em)\s+conta|dep[oó]sito\s+banc[aá]rio"
    r"|\bconta\s*(?:corrente|poupan[cç]a)?\s*[:nº.]\s*\d|\bconta\s+(?:corrente|poupan[cç]a)\b"
    r"|\bag(?:[eê]ncia|encia|enci)?\s*[:.]?\s*\d|\bc/?c\b\s*[:.]?\s*\d|\bc/c\b"
    r"|\bbanco\b|\bchave\s+pix\b|\bpix\b|\btitular\b|\bfavorecido\b|\bcpf\b",
    re.I)
_TAIL = re.compile(r"(?:[\s.;:,\-–—(]|\bobs\b)+$", re.I)


def cut_sensitive(text: str) -> str:
    """Prestadores escrevem agência, conta e Pix no campo do serviço. A descrição pára antes disso: vai para a tela."""
    m = _SENSITIVE.search(text or "")
    if not m:
        return text
    return _TAIL.sub("", text[: m.start()]).strip()


def clean_name(text: str) -> str:
    """Nome de MEI sai como "NOME 12345678909" (o CPF colado no fim): o CPF não deve aparecer na tela."""
    return re.sub(r"\s+\d{11}$", "", clean(text))


def parse_money(text: str) -> int:
    try:
        value = Decimal(text.replace(".", "").replace(",", ".")).quantize(Decimal("0.01"))
    except (InvalidOperation, AttributeError):
        raise ParseError(f"Valor inválido: {text!r}.") from None
    return int(value * 100)


def parse_date(text: str) -> date:
    try:
        d, m, y = (int(x) for x in text.split("/"))
        return date(y, m, d)
    except (ValueError, AttributeError):
        raise ParseError(f"Data inválida: {text!r}.") from None


def only_digits(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _segments(line: str) -> list[tuple[int, str]]:
    """Trechos de uma linha separados por 2+ espaços, com a coluna onde começam."""
    return [(m.start(), m.group()) for m in re.finditer(r"\S+(?: \S+)*", line)]


def _find(lines: list[str], needle: str, start: int = 0) -> int:
    needle = needle.lower()
    for i in range(start, len(lines)):
        if needle in lines[i].lower():
            return i
    return -1


def _next_nonblank(lines: list[str], i: int) -> int:
    j = i + 1
    while j < len(lines) and not lines[j].strip():
        j += 1
    return j if j < len(lines) else -1


def read_row(lines: list[str], label_idx: int) -> dict[str, str]:
    """Linha de rótulos + linha de valores logo abaixo -> {rótulo: valor}, pela coluna."""
    if label_idx < 0:
        return {}
    labels = _segments(lines[label_idx])
    j = _next_nonblank(lines, label_idx)
    out = {text: "" for _, text in labels}
    if j < 0 or not labels:
        return out
    for start, text in _segments(lines[j]):
        owners = [t for s, t in labels if s <= start + 6]
        if owners:
            key = owners[-1]
            out[key] = f"{out[key]} {text}".strip()
    return out


def pick(row: dict[str, str], *needles: str) -> str:
    for label, value in row.items():
        if any(n.lower() in label.lower() for n in needles):
            return value
    return ""


def _modulo11(digits: str) -> int:
    total, weight = 0, 2
    for ch in reversed(digits):
        total += int(ch) * weight
        weight = 2 if weight == 9 else weight + 1
    rest = total % 11
    return 0 if rest < 2 else 11 - rest


def nfe_key_is_valid(chave: str) -> bool:
    return len(chave) == 44 and chave.isdigit() and _modulo11(chave[:43]) == int(chave[43])


def _doc(text: str) -> str:
    m = re.search(_CNPJ, text) or re.search(_CPF, text)
    return m.group() if m else ""


# ---------------------------------------------------------------- DANFE (NF-e)

def parse_danfe(text: str, pages: int, plain: str = "") -> ParsedNote:
    lines = text.splitlines()
    m = re.search(r"(?<!\d)(?:\d{4}\s){10}\d{4}(?!\d)", text)
    chave = only_digits(m.group()) if m else ""
    if len(chave) != 44:
        raise ParseError("Chave de acesso do DANFE não encontrada.")
    if not nfe_key_is_valid(chave):
        raise ParseError("Chave de acesso inválida (dígito verificador não confere).")
    if chave[20:22] != "55":
        raise ParseError("Só DANFE de NF-e modelo 55 é aceito.")
    numero = str(int(chave[25:34]))
    serie = str(int(chave[22:25]))
    emit_doc_key = chave[6:20]

    # emitente: texto à esquerda da palavra DANFE, na primeira linha do cabeçalho
    emit_nome = ""
    for line in lines:
        if re.search(r"\bDANFE\b", line):
            left = line.split("DANFE")[0].strip()
            if left:
                emit_nome = clean_name(left)
                break
    m = re.search(_CNPJ, text[text.find("INSCRIÇÃO ESTADUAL") :]) if "INSCRIÇÃO ESTADUAL" in text else None
    emit_doc = m.group() if m else ""
    if emit_doc and only_digits(emit_doc) != emit_doc_key:
        raise ParseError("O CNPJ do emitente não confere com a chave de acesso.")
    if not emit_doc:
        d = emit_doc_key
        emit_doc = f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"

    # destinatário: "NOME  CNPJ/CPF  DATA DE EMISSÃO"
    tom_nome = tom_doc = ""
    emissao = None
    idx = _find(lines, "NOME / RAZÃO SOCIAL")
    j = _next_nonblank(lines, idx) if idx >= 0 else -1
    if j >= 0:
        m = re.match(rf"\s*(.*?)\s{{2,}}({_CNPJ}|{_CPF})\s+({_DATE})", lines[j])
        if m:
            tom_nome, tom_doc, emissao = clean_name(m.group(1)), m.group(2), parse_date(m.group(3))
    if emissao is None:
        raise ParseError("Data de emissão do DANFE não encontrada.")

    # totais: FRETE, SEGURO, DESCONTO, OUTRAS DESPESAS, IPI, TOTAL DA NOTA
    idx = _find(lines, "VALOR TOTAL DA NOTA")
    j = _next_nonblank(lines, idx) if idx >= 0 else -1
    nums = re.findall(_MONEY, lines[j]) if j >= 0 else []
    if len(nums) < 6:
        raise ParseError("Valor total do DANFE não encontrado.")
    valor, desconto = parse_money(nums[-1]), parse_money(nums[2])
    if valor <= 0:
        raise ParseError("O valor total da nota é zero.")

    # itens (o modo "layout" do pypdf cola as colunas da tabela de produtos; o texto simples vem inteiro)
    items: list[str] = []
    for line in (plain or text).splitlines():
        m = re.match(r"\s*(\S+)\s+(.{2,}?)\s+(\d{8})\s+\d{1,8}\s+\S+\s+\d", line)
        if m:
            items.append(clean(m.group(2), 120))
    descricao = "; ".join(items)
    if not descricao:
        i = _find(lines, "NATUREZA DA OPERAÇÃO")
        j = _next_nonblank(lines, i) if i >= 0 else -1
        descricao = clean(lines[j]) if j >= 0 else "Mercadorias"

    # vencimento (primeira parcela)
    venc = None
    idx = _find(lines, "FATURA / DUPLICATA")
    if idx >= 0:
        for line in lines[idx + 1 : idx + 6]:
            m = re.search(rf"\b\d{{3}}\s+({_DATE})\s+{_MONEY}", line)
            if m:
                venc = parse_date(m.group(1))
                break

    return ParsedNote("danfe", chave, numero, serie, emit_nome or emit_doc, emit_doc, tom_nome, tom_doc, emissao,
                      venc, clean(descricao, 500), valor, desconto, pages)


# ---------------------------------------------------------------- NFS-e (DANFSe)

def parse_nfse(text: str, pages: int) -> ParsedNote:
    lines = text.splitlines()
    m = re.search(r"(?<!\d)\d{50}(?!\d)", text)
    if not m:
        raise ParseError("Chave de acesso da NFS-e não encontrada.")
    chave = m.group()

    row = read_row(lines, _find(lines, "NÚMERO DA NFS-e"))
    numero = only_digits(pick(row, "NÚMERO DA NFS-e")).lstrip("0")
    if not numero:
        raise ParseError("Número da NFS-e não encontrado.")
    if int(chave[23:36]) != int(numero):
        raise ParseError("O número da NFS-e não confere com a chave de acesso.")
    m = re.search(_DATE, pick(row, "DATA E HORA DA EMISSÃO"))
    if not m:
        raise ParseError("Data de emissão da NFS-e não encontrada.")
    emissao = parse_date(m.group())

    serie = ""
    row_dps = read_row(lines, _find(lines, "NÚMERO DA DPS"))
    serie = clean(pick(row_dps, "SÉRIE DA DPS"), 10).strip("-")

    def party(header: str) -> tuple[str, str]:
        h = _find(lines, header)
        if h < 0:
            return "", ""
        doc = _doc(pick(read_row(lines, h), "CNPJ"))
        n = _find(lines, "Nome / Nome Empresarial", h)
        name = clean_name(pick(read_row(lines, n), "Nome")) if n >= 0 else ""
        return name, doc

    emit_nome, emit_doc = party("PRESTADOR / FORNECEDOR")
    tom_nome, tom_doc = party("TOMADOR / ADQUIRENTE")
    if chave[8] == "2" and only_digits(emit_doc) != chave[9:23]:  # prestador identificado por CNPJ na chave
        raise ParseError("O CNPJ do prestador não confere com a chave de acesso.")
    if not emit_nome:
        raise ParseError("Prestador não encontrado.")

    h = _find(lines, "Descrição do Serviço")
    desc_lines: list[str] = []
    if h >= 0:
        for line in lines[h + 1 :]:
            if re.match(r"\s*(TRIBUTAÇÃO|INFORMAÇÕES COMPLEMENTARES)", line):
                break
            if line.strip():
                desc_lines.append(line.strip())
            if len(desc_lines) >= 4:
                break
    descricao = cut_sensitive(clean(" ".join(desc_lines), 500)) or "Serviço prestado"

    op = pick(read_row(lines, _find(lines, "VALOR DA OPERAÇÃO")), "OPERAÇÃO")
    desc_inc = pick(read_row(lines, _find(lines, "VALOR DA OPERAÇÃO")), "Incondicionado")
    net_row = read_row(lines, _find(lines, "Total das Retenções"))
    net = pick(net_row, "VALOR LÍQUIDO DA NFS-e")
    mo, mn = re.search(rf"R\$\s*({_MONEY})", op), re.search(rf"R\$\s*({_MONEY})", net)
    if not (mo or mn):
        raise ParseError("Valor da NFS-e não encontrado.")
    valor = parse_money((mn or mo).group(1))
    md = re.search(rf"R\$\s*({_MONEY})", desc_inc)
    desconto = parse_money(md.group(1)) if md else 0
    if valor <= 0:
        raise ParseError("O valor da NFS-e é zero.")

    return ParsedNote("nfse", chave, numero, serie, emit_nome, emit_doc or chave[9:23], tom_nome, tom_doc, emissao,
                      None, descricao, valor, desconto, pages)


# ---------------------------------------------------------------- entrada

def parse_text(text: str, pages: int = 1, plain: str = "") -> ParsedNote:
    if re.search(r"\bDANFSe\b|Documento Auxiliar da NFS-e", text, re.I):
        return parse_nfse(text, pages)
    if re.search(r"\bDANFE\b", text) and re.search(r"Nota Fiscal Eletr", text, re.I):
        return parse_danfe(text, pages, plain)
    raise ParseError("O PDF não parece uma NFS-e nem um DANFE.")


def parse_pdf(data: bytes) -> ParsedNote:
    reader = open_pdf(data)
    text = extract_text(reader)
    if len(text.strip()) < 100:
        raise ParseError("O PDF não tem texto legível (imagem escaneada?). Envie o PDF original da nota.")
    return parse_text(text, len(reader.pages), extract_text(reader, layout=False))
