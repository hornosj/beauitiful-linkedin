# Package Desktop Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Windows desktop package that ships the Electron UI together with a bundled Python sidecar, so customer machines do not need Python installed.

**Architecture:** Keep development mode unchanged: Electron launches `python -m beautiful_linkedin.server` from the repo. In packaged production, Electron launches `resources/sidecar/beautiful-linkedin-sidecar.exe`, which is produced by PyInstaller and copied into the installer by electron-builder.

**Tech Stack:** Electron, electron-vite, electron-builder, PyInstaller, FastAPI sidecar, PowerShell build helper.

---

### Task 1: Sidecar Launch Resolution

**Files:**
- Modify: `electron/src/main/sidecar.ts`
- Test: `electron/tests/sidecar.test.ts`

- [ ] Add command resolution that prefers the packaged sidecar executable in production.
- [ ] Preserve the current Python command fallback for development.
- [ ] Cover command selection with unit tests for dev, explicit Python override, and packaged resource path.

### Task 2: Electron Builder Packaging

**Files:**
- Modify: `electron/package.json`
- Modify: `electron/package-lock.json`

- [ ] Add `electron-builder` as a dev dependency.
- [ ] Add `dist:win` and `pack:win` scripts.
- [ ] Add electron-builder config that includes `out/**/*`, `package.json`, and `../dist/beautiful-linkedin-sidecar` as `extraResources/sidecar`.
- [ ] Configure Windows NSIS output under `electron/release`.

### Task 3: Sidecar Build Helper

**Files:**
- Create: `scripts/build-sidecar.ps1`

- [ ] Add a PowerShell helper that runs PyInstaller from the active Python environment.
- [ ] Build `src/beautiful_linkedin/server/__main__.py` as `dist/beautiful-linkedin-sidecar/beautiful-linkedin-sidecar.exe`.
- [ ] Use `--onedir`, `--console`, `--paths src`, and package collection flags for the local project.

### Task 4: Verification

**Commands:**
- `pytest tests -q`
- `cd electron; npm run typecheck`
- `cd electron; npm test`
- `powershell -ExecutionPolicy Bypass -File scripts/build-sidecar.ps1`
- `cd electron; npm run build`

- [ ] Run Python tests.
- [ ] Run Electron typecheck and tests.
- [ ] Build the sidecar.
- [ ] Build the Electron app.
- [ ] If the full installer cannot be built in this environment, report the blocker and leave the package scripts ready.
