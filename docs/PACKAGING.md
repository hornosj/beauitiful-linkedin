# Empacotamento Desktop

Este fluxo gera instaladores desktop com o Electron e o sidecar Python
embutido. O cliente final nao precisa ter Python, Node.js ou npm instalados.

## Pre-requisitos

- Windows.
- Node.js com `npm`.
- Python 3.11 ou 3.12 recomendado para builds de release.
- macOS 12+ para gerar builds macOS.
- Xcode Command Line Tools no macOS (`xcode-select --install`).

O projeto roda em Python 3.14 neste checkout, mas `rookiepy` pode falhar ao
instalar do zero nessa versao por causa do limite atual de PyO3 usado pela
dependencia. Para um build reproduzivel de release, prefira Python 3.11/3.12.

## Instalar dependencias

Na raiz:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[server,dev,risky,scrapling]"
```

No Electron:

```powershell
cd electron
npm install
cd ..
```

## Validar

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
cd electron
npm run typecheck
npm test
cd ..
```

## Gerar sidecar Python

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-sidecar.ps1
```

Saida esperada:

```text
dist\beautiful-linkedin-sidecar\beautiful-linkedin-sidecar.exe
```

macOS:

```bash
bash scripts/build-sidecar.sh
```

Saida esperada:

```text
dist/beautiful-linkedin-sidecar/beautiful-linkedin-sidecar
```

O sidecar precisa ser gerado no mesmo sistema operacional do instalador. Nao
use o `.exe` do Windows para montar o `.dmg` do macOS.

## Gerar instalador Windows

```powershell
cd electron
npm run dist:win
```

Saida esperada:

```text
electron\release\Beautiful LinkedIn Setup 0.1.0.exe
```

## Gerar instalador macOS

No macOS, depois de gerar o sidecar com `scripts/build-sidecar.sh`:

```bash
cd electron
npm run dist:mac
```

Saida esperada:

```text
electron/release/Beautiful LinkedIn-0.1.0.dmg
electron/release/Beautiful LinkedIn-0.1.0-mac.zip
```

O app macOS mantem a mesma arquitetura do Windows: Electron abre a UI React,
inicia o sidecar FastAPI por loopback e usa o Chromium embutido do Electron
para o fluxo de browser interno/CDP. O usuario final precisa apenas do app
instalado, conexao com internet e credenciais/chaves opcionais que forem usadas
nos provedores.

## Build unsigned

O build atual desativa assinatura/edicao de executaveis no `electron-builder`
para permitir pacote local sem certificado. Isso e adequado para piloto.

No macOS, builds sem assinatura/notarizacao podem disparar o Gatekeeper. Para
distribuicao publica, use uma conta Apple Developer, assine o app com Developer
ID e faca notarizacao antes de publicar. Para teste local, o operador pode abrir
o app pelo menu de contexto do Finder depois de confirmar o aviso de seguranca.

## Requisitos do usuario final

- Windows 10/11 ou macOS 12+.
- Conexao com internet para buscar leads e consultar provedores.
- Login manual nos servicos usados pelo operador quando aplicavel.
- Variaveis/chaves opcionais em `.env` ou na tela de Preferencias: SearxNG,
  Serper, Brave, Google CSE, Apollo, Lusha, Snov.io, PDL, Coresignal, Apify e
  Telegram API ID/hash para o fluxo Telethon.

Python, Node.js, npm, PyInstaller e dependencias de desenvolvimento sao
necessarios apenas para quem vai gerar o instalador.
