"""PDFs sintéticos para os testes: o texto-modelo (anonimizado) vira um PDF em Courier, coluna por coluna."""
from __future__ import annotations

import re
from pathlib import Path

FIX = Path(__file__).parent / "fixtures"


def nfe_key(uf="29", aamm="2603", cnpj="11222333000181", mod="55", serie=1, numero=316, cnf="08293881") -> str:
    body = f"{uf}{aamm}{cnpj}{mod}{serie:03d}{numero:09d}1{cnf}"
    total, w = 0, 2
    for ch in reversed(body):
        total += int(ch) * w
        w = 2 if w == 9 else w + 1
    r = total % 11
    return body + str(0 if r < 2 else 11 - r)


def nfse_key(numero=1, cnpj="11222333000181", aamm="2605") -> str:
    return "2927705" + "2" + "2" + cnpj + f"{numero:013d}" + aamm + "4446416094"


def danfe_text(numero=316, total="1.400,00", desconto="100,00", cnpj="11222333000181") -> str:
    key = nfe_key(numero=numero, cnpj=cnpj)
    t = (FIX / "danfe.txt").read_text()
    t = t.replace("@CHAVE4@", " ".join(key[i:i + 4] for i in range(0, 44, 4)))
    return t.replace("@NUM@", str(numero)).replace("@TOTAL@", total).replace("@DESCONTO@", desconto)


def nfse_text(numero=1, total="1.300,00", cnpj="11222333000181") -> str:
    t = (FIX / "nfse.txt").read_text()
    return t.replace("@CHAVE@", nfse_key(numero, cnpj)).replace("@NUM@", str(numero)).replace("@TOTAL@", total)


def make_pdf(text: str, extra: bytes = b"", size: float = 5.0, catalog_extra: bytes = b"", page_extra: bytes = b"",
             extra_objects: tuple = ()) -> bytes:
    lines = text.splitlines()
    width = max(len(line) for line in lines) * size * 0.6 + 40
    height = len(lines) * (size + 1) + 40
    ops = []
    cw = size * 0.482  # calibrado: o modo layout do pypdf reconstrói as colunas com esta largura de caractere
    for i, line in enumerate(lines):
        y = height - 20 - i * (size + 1)
        for m in re.finditer(r"\S+(?: \S+)*", line):  # cada trecho vai na sua coluna, como no PDF real
            raw = m.group().encode("cp1252", errors="replace")
            esc = b"".join(bytes([c]) if 32 <= c < 127 and chr(c) not in "\\()" else b"\\%03o" % c for c in raw)
            ops.append(b"BT /F1 %.1f Tf %.2f %.1f Td (%s) Tj ET" % (size, 20 + m.start() * cw, y, esc))
    stream = b"\n".join(ops)
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R " + catalog_extra + b" >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.1f %.1f] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R " % (width, height) + page_extra + b" >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        *extra_objects,
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref) + extra
    return bytes(out)


def danfe_pdf(**kw) -> bytes:
    return make_pdf(danfe_text(**kw))


def nfse_pdf(**kw) -> bytes:
    return make_pdf(nfse_text(**kw))
