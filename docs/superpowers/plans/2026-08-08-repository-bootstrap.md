# ARD Ossie Provider Repository Bootstrap Implementation Plan

> **Superseded policy (2026-08-16):** Public visibility requirements in this historical plan are
> replaced by the private repository and Issue intake contract in
> `docs/superpowers/specs/2026-08-16-private-repository-issue-intake-auth-design.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish the approved ARD Ossie architecture and repository governance files to the empty `kimohy/ard-ossie-provider` GitHub repository.

**Architecture:** Keep the initial repository documentation-only so implementation choices remain governed by the approved design. Initialize `main` with one root file, then publish README, Apache-2.0 license, Git/LFS policy, architecture specification, and source inventory in a single follow-up commit.

**Tech Stack:** GitHub Git Data API, Markdown, Git LFS attributes

## Global Constraints

- Target repository is `kimohy/ard-ossie-provider`.
- Preserve the repository's public visibility and `main` default branch.
- Do not publish `.superpowers/` brainstorming files, local logs, caches, or secrets.
- Track PDF, DOCX, XLSX, XLS, and DOC source files with Git LFS.
- Do not add implementation code in the bootstrap change.

---

### Task 1: Initialize the empty repository

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: approved architecture specification
- Produces: initial `main` commit and repository entry page

- [ ] **Step 1: Confirm repository state**

Verify `kimohy/ard-ossie-provider` exists, uses `main`, is empty, and grants push permission.

- [ ] **Step 2: Create the root commit**

Create `README.md` through the GitHub contents API with commit message `docs: initialize ARD Ossie provider`.

- [ ] **Step 3: Verify the root file**

Fetch `README.md` from `main` and confirm it includes `ARD Ossie Provider` and the architecture document link.

### Task 2: Publish governance and architecture documents

**Files:**
- Create: `.gitignore`
- Create: `.gitattributes`
- Create: `LICENSE`
- Create: `docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md`
- Create: `docs/superpowers/specs/references/source-inventory.md`
- Create: `docs/superpowers/plans/2026-08-08-repository-bootstrap.md`

**Interfaces:**
- Consumes: root commit SHA and local approved documents
- Produces: one tree commit containing all repository bootstrap files

- [ ] **Step 1: Create blobs for each intended file**

Upload exactly the six files listed above and record every returned blob SHA.

- [ ] **Step 2: Create a tree based on the root commit**

Build a tree from the root commit's tree and add each blob at its exact repository path with mode `100644` and type `blob`.

- [ ] **Step 3: Create the bootstrap commit**

Create commit `docs: add architecture and repository policy` with the root commit as its only parent.

- [ ] **Step 4: Fast-forward main**

Update `refs/heads/main` to the bootstrap commit with `force=false`.

### Task 3: Verify published state

**Files:**
- Verify: `README.md`
- Verify: `.gitignore`
- Verify: `.gitattributes`
- Verify: `LICENSE`
- Verify: `docs/superpowers/specs/2026-08-08-ai-ready-data-ossie-architecture-design.md`
- Verify: `docs/superpowers/specs/references/source-inventory.md`
- Verify: `docs/superpowers/plans/2026-08-08-repository-bootstrap.md`

**Interfaces:**
- Consumes: published `main` branch
- Produces: evidence that all intended files are readable and no local-only artifacts were published

- [ ] **Step 1: Fetch all expected paths**

Fetch each file from `main`; every request must succeed.

- [ ] **Step 2: Check critical content**

Confirm the architecture document contains the Ossie 0.1.1 target, stable product/table IDs, many-to-many table mapping, and OpenAI-compatible provider design.

- [ ] **Step 3: Confirm repository metadata**

Verify default branch remains `main`, visibility remains public, and the repository is no longer empty.
