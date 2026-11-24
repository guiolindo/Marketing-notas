# Notas do Marketing de Porto Seguro

Sistema web para lançar **NFS-e** e **DANFE** (PDF), somar os valores e consultar tudo com filtros.
Qualquer pessoa com o link vê as notas, filtra, baixa uma nota ou **todas num PDF só**.
Só o administrador (`/admin`) lança, classifica e exclui notas.

Stack: FastAPI · SQLAlchemy 2 · Jinja2 · pypdf · openpyxl · PostgreSQL (Railway) / SQLite (dev).
O visual (tokens, tipografia, componentes, tela de acesso) vem do repositório `Agua-e-luz---economart`.

## Como funciona

1. Em `/admin` o administrador entra com usuário e senha e arrasta um ou vários PDFs.
2. O sistema lê o texto do PDF (sem OCR, sem serviço externo): tipo, número, chave de acesso, emissão,
   fornecedor, tomador, descrição, vencimento, desconto e **valor**.
3. O PDF original fica guardado no banco. A página inicial soma o valor, mostra os totais por mês e por
   fornecedor e permite filtrar por texto, tipo, fornecedor, categoria, período e faixa de valor.
4. **Baixar tudo em PDF** (botão no topo) junta os PDFs em um só, em ordem de emissão, com um marcador por nota.
   O botão respeita os filtros ativos: filtrou um fornecedor, baixa só as notas dele. Há também a planilha Excel.

**Aviso no topo do site**: em `/admin` → "Aviso no topo do site" o administrador escolhe uma mensagem pronta
("Estamos procurando as notas e colocando para download", "Todas as notas estão disponíveis", "Novas notas estão sendo
adicionadas"), escreve a sua (até 200 caracteres, com cor azul/laranja/verde) ou desliga. O aviso aparece para todos os
visitantes, fica salvo no banco (sobrevive a deploys) e muda em até 15 segundos.

**Dispositivos**: o `/admin` mostra quantos aparelhos diferentes abriram o site (total, hoje, últimos 7 dias), por tipo
(celular/tablet/computador), navegador, sistema e **modelo** (só quando o navegador informa: Chrome/Edge no Android, via
`Sec-CH-UA-Model`; iPhone e Safari não informam). A contagem é **anônima**: cada aparelho recebe um código aleatório num
cookie próprio e só o *hash* dele é guardado. **Não** se grava IP, localização nem o texto bruto do navegador. Robôs,
`/health`, estáticos, PDFs e erros não contam, nem quem envia o sinal `Sec-GPC`. O cartão também diz se o aparelho de quem
está vendo foi contado e, se não, o motivo.

**Lançamento e pagamento (conciliação com o ERP)**: cada nota pode ter parcelas (títulos do contas a pagar) com
vencimento, data e valor pago. A situação aparece no site (Paga · Paga em parte · A pagar · Sem lançamento), com filtro,
totais (Pago / A pagar / Sem lançamento) e nas exportações. O fluxo, em `/admin` → **Conciliação**:

1. **Dados do sistema (.xlsx)**: exporta todas as notas (id, chave como texto, número, número curto, CNPJ, valores) e as
   instruções do formato (aba LEIA-ME).
2. Esse arquivo + a planilha do ERP vão para o Claude, que roda `scripts/conciliar.py` (CNPJ obrigatório; pontua valor,
   emissão e número, inclusive número com zeros cortados e TÍTULO que o Excel transformou em data), revisa os casos
   duvidosos e devolve o **arquivo de conciliação** (abas `conciliacao` e `sem_pdf`).
3. **Importar** → tela de conferência (o que muda em cada nota, erros, avisos) → **Aplicar**. Erros bloqueiam a aplicação;
   cada nota do arquivo tem as parcelas substituídas (reimportar não duplica). Títulos do ERP sem PDF no sistema ficam
   listados no `/admin` para lançar os PDFs que faltam.

**Notas aguardando PDF**: os títulos do ERP sem PDF (aba `sem_pdf` da última conciliação) aparecem para todos num cartão
com a mensagem do admin (padrão "Solicitamos por e-mail ao CDI."), editável e com opção de esconder em `/admin` →
Conciliação. A lista diminui sozinha quando os PDFs forem lançados e conciliados.
Para PDF **escaneado** (sem texto, sem OCR), o admin usa **Vincular PDF** no próprio item: escolhe o arquivo, confere tipo e
número (pré-preenchidos pelo título) e, se legível, digita a chave. A nota é criada com os dados do ERP (fornecedor, CNPJ,
emissão, valor, lançamento, vencimento, pagamento; parcelas da mesma nota entram juntas), marcada como "PDF escaneado", e
sai da lista. O PDF passa pelas mesmas travas de segurança e duplicidade do envio normal.
Título que não é da loja (lançado errado no ERP) sai com **Remover da lista** (com motivo); o sistema lembra disso e ele
não volta nas próximas conciliações. A lista dos removidos fica no cartão Conciliação, com **Restaurar**.

**Exportações** (topo do site, respeitam os filtros): PDF único e **planilha Excel** (.xlsx com abas Notas e Parcelas,
chave e número como texto, datas e valores formatados, linha de total; texto nunca vira fórmula).

Layouts aceitos: **DANFSe nacional** (NFS-e) e **DANFE de NF-e modelo 55**. PDF escaneado (imagem), NFC-e e
outros layouts são recusados com a mensagem do motivo, em vez de gravar um valor errado.

### Bloqueio de duplicidade (três camadas)

| Camada | Pega |
|---|---|
| SHA-256 do arquivo | o mesmo PDF enviado duas vezes |
| Chave de acesso (44/50 dígitos) | a mesma nota em PDFs diferentes (reimpressão, outro visualizador) |
| Tipo + CNPJ do emitente + série + número | a mesma nota sem chave legível |

As três também são `UNIQUE` no banco: dois envios simultâneos não passam. Excluir uma nota libera o reenvio.

### Conferências de integridade

- DANFE: dígito verificador (módulo 11) da chave, modelo 55, CNPJ e número da chave batem com o impresso.
- NFS-e: CNPJ do prestador e número batem com a chave.
- Se qualquer campo essencial (chave, valor, data) não for encontrado, o arquivo é recusado.

## Deploy no Railway

1. **New Project → Deploy from GitHub repo** e escolha este repositório (o Railway usa `railway.json`).
2. **+ New → Database → PostgreSQL** no mesmo projeto.
3. No serviço web, **Variables → Raw Editor**, cole o conteúdo de [`railway.env.example`](railway.env.example) e preencha
   `SECRET_KEY` e `ADMIN_PASSWORD` (e, se quiser, `VIEWER_PASSWORD`).
4. **Settings → Networking → Generate Domain**. O administrador entra em `https://SEU-DOMINIO/admin`.
5. Confira no log: o start deve terminar em `Application startup complete`. O healthcheck é `/health`.

As tabelas são criadas na primeira subida. O disco do Railway não persiste entre deploys, por isso os PDFs ficam
**no PostgreSQL** (ative os backups do plugin).

### Variáveis

| Variável | Obrigatória | Para quê |
|---|---|---|
| `DATABASE_URL` | sim | PostgreSQL (`${{Postgres.DATABASE_URL}}`). Sem ela o servidor **não sobe** em produção (o SQLite do contêiner some a cada deploy) |
| `SECRET_KEY` | sim | assina a sessão (≥ 32 caracteres) |
| `ADMIN_PASSWORD` | sim | senha do `/admin` (≥ 12 caracteres, letras + números/símbolos) |
| `ADMIN_USERNAME` | não | padrão `admin` |
| `VIEWER_PASSWORD` | não | exige senha também para **ver** as notas |
| `DOCUMENT_ENCRYPTION_KEY` | não | criptografa os PDFs no banco (Fernet). Perdeu a chave, perdeu os PDFs |
| `MAX_UPLOAD_MB` / `MAX_FILES_PER_UPLOAD` | não | 10 MB / 200 arquivos por requisição (o navegador envia em lotes de 10, sem limite de quantidade) |
| `MAX_MERGED_PAGES` | não | teto de páginas do PDF único (2500) |
| `MERGE_CONCURRENCY` | não | PDFs únicos gerados ao mesmo tempo (2) |
| `MERGE_RATE_PER_MINUTE` | não | downloads pesados (PDF único/planilha) por IP por minuto (6) |
| `MERGE_CACHE_MB` | não | cache em memória dos PDFs únicos já gerados (64; 0 desliga) |
| `TRUSTED_PROXY_COUNT` | não | proxies na frente do app (Railway = 1) |
| `DEBUG` | não | `true` só em desenvolvimento |

Com `DEBUG=false` o servidor **recusa subir** se `SECRET_KEY` ou `ADMIN_PASSWORD` estiverem ausentes ou fracas.

## Celular

Abaixo de 768 px a tela é pensada para rolar só para cima e para baixo (padrão do `elei-es`): topo compacto com área segura
do aparelho, abas fixas embaixo (Notas / Lançar / Sair), filtros recolhidos atrás de um botão, cada nota como um cartão
empilhado com o valor em destaque, campos com 16 px (o iPhone não dá zoom ao tocar) e `overflow-x: clip` como rede de
segurança. Ao mexer no CSS, confira em celular emulado (`isMobile`) que `scrollWidth <= clientWidth` em todas as telas.

## Desempenho e proteção contra falhas

Técnicas adaptadas do `notas-despesas` (teto de páginas, falha explícita, limites nas rotas caras):

- **Cache do PDF único**: a chave é o conjunto de notas (id + hash do arquivo) em ordem. Pedir o mesmo pacote de novo
  não relê o banco nem junta nada. Entrou, saiu ou foi trocado um PDF: chave nova. Editar categoria não invalida.
- **Teto de páginas** (`MAX_MERGED_PAGES`): acima dele o download é recusado (413) com orientação para filtrar,
  em vez de entregar um pacote cortado em silêncio.
- **Falha explícita**: se algum PDF guardado não puder ser juntado, o download falha dizendo qual nota. Pacote incompleto
  de nota fiscal passaria por completo.
- **Rotas caras protegidas**: PDF único e planilha são públicos, então têm limite por IP (429) e no máximo
  `MERGE_CONCURRENCY` gerações simultâneas (503 + nova tentativa), para ninguém esgotar a memória do servidor.
- **PDF menor quando possível**: fontes e imagens idênticas repetidas entre notas são unificadas. Se a otimização
  falhar, o PDF sai sem ela.
- **HTTP**: gzip para HTML/CSS/JS (PDFs não, já são comprimidos); CSS/JS com versão por **hash do conteúdo** e
  cache de 1 ano (`immutable`); fontes imutáveis.
- **Banco**: o PDF fica numa coluna carregada sob demanda; listar, filtrar e somar nunca lê os arquivos.

## Segurança

- **Login do administrador**: senha só em variável de ambiente (guardada em memória como hash scrypt), comparação em tempo
  constante, mensagem única para usuário ou senha errados, atraso nas falhas e **trava por IP** (5 falhas em 15 min → 429).
- **Sessão**: cookie assinado, `HttpOnly`, `Secure`, `SameSite=Lax`, prefixo `__Host-`; expira em 8 h ou 60 min parado;
  sessão nova a cada login (sem fixação).
- **CSRF**: token em todo formulário que altera dados + verificação de `Origin`.
- **Upload**: só PDF (assinatura `%PDF-`), limite de tamanho e de quantidade, no máximo 10 páginas, recusa PDF protegido por
  senha e PDF com JavaScript, anexos, ações de abertura ou lançamento de programas. No PDF único, anotações/links dos
  originais são removidos.
- **Cabeçalhos**: CSP sem `unsafe-inline`, `X-Frame-Options: DENY`, `nosniff`, HSTS, `noindex` + `robots.txt`.
- **Banco**: consultas parametrizadas (SQLAlchemy), busca com escape de `%`/`_`, ordenação por lista fixa; na planilha Excel
  nenhum texto vira fórmula (ex.: `=HYPERLINK(...)` sai como texto). Textos vindos do PDF são escapados na tela.
- **Auditoria**: logins, falhas, envios, edições e exclusões (com IP) aparecem em "Atividade recente" no `/admin`.
- **Sem dependência externa em tempo de execução**: fontes e gráficos são servidos pelo próprio sistema, sem CDN.

> **Atenção sobre privacidade.** Os PDFs são documentos fiscais completos. Uma NFS-e de MEI, por exemplo, pode trazer
> CPF e conta bancária em "Informações complementares", e o PDF original é exatamente o que fica disponível para
> download. Se o link não deve ser público, defina `VIEWER_PASSWORD`. O CPF que alguns MEIs têm colado ao nome
> (`NOME 12345678909`) é removido do nome exibido na tela, mas continua no PDF.

## Desenvolvimento

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env     # DEBUG=true, SECRET_KEY e ADMIN_PASSWORD
python run.py            # http://127.0.0.1:8000  ·  http://127.0.0.1:8000/admin

ruff check --select F,E9 .
python -m pytest -q                          # SQLite
TEST_DATABASE_URL=postgresql://... pytest -q # contra um PostgreSQL de verdade
```

Os testes não usam PDFs reais: `tests/fixtures/*.txt` são os textos dos dois layouts, anonimizados e com marcadores, e
`tests/helpers.py` monta PDFs sintéticos com chaves de acesso válidas.

```
app/
  main.py          fábrica do app, middlewares e tratamento de erros
  routes.py        páginas públicas e /admin
  security.py      sessão, login, trava, CSRF, cabeçalhos
  parsing.py       leitura do PDF (NFS-e / DANFE) e validação do arquivo
  services/notes.py lançamento + duplicidade, filtros, totais
  models.py        Note, LoginAttempt, AuditLog
  templates/ static/
```

## Limitações conhecidas

- Só lê PDFs com texto selecionável (o original do emissor). Foto ou PDF escaneado é recusado.
- Layouts de NFS-e municipais fora do padrão nacional (DANFSe) não são reconhecidos.
- Sem migrações de banco (Alembic): as tabelas são criadas na subida. Para mudar o esquema no futuro, adicione migrações.
- A trava de login conta por IP; atrás de um proxy diferente do Railway ajuste `TRUSTED_PROXY_COUNT`.
