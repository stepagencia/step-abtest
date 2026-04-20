"""
STEP - Sincronização automática de Testes A/B de Conteúdo
==========================================================

FLUXO 1 — Detectar novos testes A/B
  Busca tarefas nas 4 listas do fluxo de produção (Planejamento, Copy,
  Design, Agendamentos) que tenham a etiqueta `executar teste` e AINDA
  NÃO foram processadas (sem a etiqueta `teste processado`).

  Para cada uma (Tarefa 1):
    1. Lê os custom fields (Cliente, Rede Social, Tipo, Editorias,
       Link do Post Original, Data da Postagem, Legenda) e "Tipo de teste".
    2. RECUPERAÇÃO: se a T1 já tem alguma tarefa vinculada com tag
       `teste a/b`, pula (execução anterior falhou no meio — só marca
       `teste processado` e segue).
    3. Cria a Tarefa 2 na lista Planejamento com etiqueta `teste a/b`,
       copiando todos os custom fields, nome `[TESTE A/B - {tipo}] {orig}`.
    4. Cria a Tarefa 3 na lista "Testes A/B de Conteúdo" com os mesmos
       campos + "Tipo de teste" preenchido + relacionamento
       "Planejamento" apontando para T2.
    5. Vincula T1↔T2, T2↔T3, T1↔T3 (link bidirecional do ClickUp).
    6. Adiciona `teste processado` na T1 e remove `executar teste`.

FLUXO 2 — Sincronizar status e data da Tarefa 3
  Varre Tarefas 2 (tag `teste a/b`) em todas as 4 listas do fluxo e
  alinha o status da T3:
    - T2 em Planejamento     → T3 "adicionado ao planejamento"
    - T2 em Copy ou Design   → T3 "em produção"
    - T2 em Agendamentos     → T3 "análise" + due_date = Data da Postagem
  Se a T3 já estiver em "teste completo" ou "inconclusivo" (estados
  terminais), NÃO mexe mais.

Segurança
---------
- API token vem da env var CLICKUP_API_TOKEN
- DRY_RUN=1 → só loga o que faria, sem tocar na API
- Idempotente: tag `teste processado` impede reprocessamento

Como rodar localmente:
    export CLICKUP_API_TOKEN="pk_..."
    export DRY_RUN=1            # opcional, pra teste seco
    python ab_test_sync.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Configuração — IDs do workspace da STEP
# ---------------------------------------------------------------------------

WORKSPACE_ID = "9013038195"

# Listas do fluxo de conteúdo
LIST_PLANEJAMENTO = "901306281641"       # Planejamento de conteúdo N1
LIST_COPY = "901306281633"               # Copy conteúdo
LIST_DESIGN = "901306281639"             # Design/Edição
LIST_AGENDAMENTOS = "901306281642"       # Agendamentos
LIST_TESTE_AB = "901326648620"           # Testes A/B de Conteúdo

LISTAS_FLUXO = [LIST_PLANEJAMENTO, LIST_COPY, LIST_DESIGN, LIST_AGENDAMENTOS]

# Etiquetas (tags)
TAG_EXECUTAR_TESTE = "executar teste"
TAG_TESTE_PROCESSADO = "teste processado"
TAG_TESTE_AB = "teste a/b"

# Custom field IDs (globais do workspace)
CF_CLIENTE = "e41a916f-7818-44b6-9e93-fb003f52ad53"
CF_REDE_SOCIAL = "5293fb4f-2741-4aab-bb1c-518e9e1d2030"
CF_TIPO = "4fc73c67-8c6e-4e73-ad8e-885df6586260"
CF_EDITORIAS = "7a155e2e-5b70-467c-894f-98f7f4cc1722"
CF_LINK_POST_ORIGINAL = "3ee94567-b2f1-4819-91f9-726fcb4378c0"
CF_DATA_POSTAGEM = "4ccefbf6-8e46-48af-8f94-1f3eeb8770f6"
CF_LEGENDA = "322837ee-3eba-41a8-8a5e-82b61fa15366"
CF_PLANEJAMENTO_REL = "5addfdcc-5182-4547-9d3a-89bd31094118"
CF_TIPO_TESTE = "273dcb9f-81ee-49bc-b0ec-9ef169bccceb"

# Campos copiados T1 → T2 → T3
COPIABLE_FIELDS = [
    CF_CLIENTE,
    CF_REDE_SOCIAL,
    CF_TIPO,
    CF_EDITORIAS,
    CF_LINK_POST_ORIGINAL,
    CF_DATA_POSTAGEM,
    CF_LEGENDA,
]

DROPDOWN_FIELDS = {CF_CLIENTE, CF_REDE_SOCIAL, CF_TIPO, CF_EDITORIAS}

# Status da lista Testes A/B — ClickUp exige minúsculo no PUT
STATUS_T3_PLANEJAMENTO = "adicionado ao planejamento"
STATUS_T3_EM_PRODUCAO = "em produção"
STATUS_T3_ANALISE = "análise"
STATUS_T3_TERMINAIS = {"teste completo", "inconclusivo"}

# ---------------------------------------------------------------------------
# Cliente ClickUp
# ---------------------------------------------------------------------------

log = logging.getLogger("ab_test_sync")

API_BASE = "https://api.clickup.com/api/v2"


class ClickUp:
    """Wrapper sobre a API do ClickUp com retry e DRY_RUN."""

    def __init__(self, token: str, dry_run: bool = False) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
        })
        self.dry_run = dry_run

    def _req(self, method: str, path: str, *, write: bool = False,
             **kwargs: Any) -> Any:
        if write and self.dry_run:
            log.info("    [DRY_RUN] %s %s %s", method, path,
                     kwargs.get("json") or kwargs.get("params") or "")
            # Em dry-run retornamos um stub plausível
            if method == "POST" and "/task" in path and path.endswith("/task"):
                return {"id": "DRYRUN_NEW_TASK", "name": "[dry-run]"}
            return {}
        for attempt in range(3):
            resp = self.session.request(method, f"{API_BASE}{path}",
                                        timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limit, aguardando %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log.error("ClickUp %s %s -> %s: %s",
                          method, path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
            return resp.json() if resp.text else None
        resp.raise_for_status()

    # ----- Leitura -----

    def list_tasks(self, list_id: str, **params: Any) -> list[dict]:
        """Pagina todas as tarefas de uma lista, incluindo fechadas."""
        out: list[dict] = []
        page = 0
        base = {
            "subtasks": "true",
            "include_closed": "true",
            "archived": "false",
        }
        while True:
            data = self._req("GET", f"/list/{list_id}/task",
                             params={**base, **params, "page": page})
            tasks = data.get("tasks", []) if data else []
            out.extend(tasks)
            if len(tasks) < 100:
                break
            page += 1
        return out

    def filter_team_tasks(self, list_ids: list[str], tags: list[str]) -> list[dict]:
        """Filtered search via /team/{team_id}/task — muito mais rápido que
        paginar list_tasks inteira quando queremos só tarefas com tag específica.

        Tarefas publicadas ficam 'closed'; include_closed=true é obrigatório
        pois a usuária pode disparar testes em conteúdo já publicado."""
        out: list[dict] = []
        page = 0
        # requests aceita lista → gera list_ids[]=X&list_ids[]=Y
        params: list[tuple[str, Any]] = [
            ("subtasks", "true"),
            ("include_closed", "true"),
            ("archived", "false"),
        ]
        for lid in list_ids:
            params.append(("list_ids[]", lid))
        for tag in tags:
            params.append(("tags[]", tag))

        while True:
            page_params = params + [("page", page)]
            data = self._req("GET", f"/team/{WORKSPACE_ID}/task",
                             params=page_params)
            tasks = data.get("tasks", []) if data else []
            out.extend(tasks)
            if len(tasks) < 100:
                break
            page += 1
        return out

    def get_task(self, task_id: str) -> dict:
        return self._req("GET", f"/task/{task_id}",
                         params={"include_subtasks": "false"})

    # ----- Escrita -----

    def create_task(self, list_id: str, payload: dict) -> dict:
        return self._req("POST", f"/list/{list_id}/task",
                         json=payload, write=True)

    def update_task(self, task_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/task/{task_id}",
                         json=payload, write=True)

    def set_custom_field(self, task_id: str, field_id: str, value: Any) -> None:
        self._req("POST", f"/task/{task_id}/field/{field_id}",
                  json={"value": value}, write=True)

    def add_tag(self, task_id: str, tag: str) -> None:
        self._req("POST", f"/task/{task_id}/tag/{tag}", write=True)

    def remove_tag(self, task_id: str, tag: str) -> None:
        self._req("DELETE", f"/task/{task_id}/tag/{tag}", write=True)

    def link_tasks(self, task_id: str, links_to: str) -> None:
        self._req("POST", f"/task/{task_id}/link/{links_to}", write=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cf_value(task: dict, field_id: str) -> Any:
    """Valor bruto de um custom field. Cuidado: 0 e '' são válidos."""
    for cf in task.get("custom_fields", []):
        if cf["id"] == field_id:
            return cf.get("value")
    return None


def dropdown_option_id(task: dict, field_id: str) -> Optional[str]:
    """UUID da opção selecionada num dropdown. Trata `value is None`
    explicitamente (0 é orderindex válido — Headline)."""
    for cf in task.get("custom_fields", []):
        if cf["id"] != field_id:
            continue
        val = cf.get("value")
        if val is None:
            return None
        options = cf.get("type_config", {}).get("options", [])
        if isinstance(val, int):
            if 0 <= val < len(options):
                return options[val]["id"]
            return None
        if isinstance(val, str):
            return val or None
    return None


def dropdown_option_name(task: dict, field_id: str) -> Optional[str]:
    """Nome da opção selecionada."""
    opt_id = dropdown_option_id(task, field_id)
    if not opt_id:
        return None
    for cf in task.get("custom_fields", []):
        if cf["id"] != field_id:
            continue
        for opt in cf.get("type_config", {}).get("options", []):
            if opt.get("id") == opt_id:
                return opt.get("name")
    return None


def tag_names(task: dict) -> set[str]:
    return {t["name"] for t in task.get("tags", [])}


def apply_custom_fields(cu: ClickUp, task_id: str, source: dict,
                        *, extra_tipo_teste_id: Optional[str] = None) -> None:
    """Copia campos da tarefa original para a nova, um por um."""
    for field_id in COPIABLE_FIELDS:
        raw = cf_value(source, field_id)
        if raw is None or raw == "":
            continue
        try:
            if field_id in DROPDOWN_FIELDS:
                opt_id = dropdown_option_id(source, field_id)
                if opt_id:
                    cu.set_custom_field(task_id, field_id, opt_id)
            else:
                cu.set_custom_field(task_id, field_id, raw)
        except Exception as exc:  # noqa: BLE001
            log.warning("    Falha ao preencher %s em %s: %s",
                        field_id, task_id, exc)

    if extra_tipo_teste_id:
        try:
            cu.set_custom_field(task_id, CF_TIPO_TESTE, extra_tipo_teste_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("    Falha ao preencher Tipo de teste em %s: %s",
                        task_id, exc)


def find_linked_t3_in_testeab(task: dict, testeab_ids: set[str]) -> Optional[str]:
    """Encontra linked task que está na lista Testes A/B.
    Usa um set pré-carregado pra não fazer GET individual."""
    for link in task.get("linked_tasks", []):
        cand_id = link.get("task_id") or link.get("link_id")
        if not cand_id or cand_id == task["id"]:
            continue
        if cand_id in testeab_ids:
            return cand_id
    return None


def t1_already_has_variacao(t1: dict, tag_ab_ids: set[str]) -> bool:
    """Se T1 já tem linked task que está no conjunto de tarefas com tag
    'teste a/b', execução anterior já criou a T2. Evita duplicar."""
    for link in t1.get("linked_tasks", []):
        cand_id = link.get("task_id") or link.get("link_id")
        if not cand_id or cand_id == t1["id"]:
            continue
        if cand_id in tag_ab_ids:
            return True
    return False


# ---------------------------------------------------------------------------
# FLUXO 1 — cria T2 e T3 a partir de T1
# ---------------------------------------------------------------------------

def process_executar_teste(cu: ClickUp, tasks_by_list: dict[str, list[dict]],
                           testeab_ids: set[str],
                           tag_ab_ids: set[str]) -> None:
    candidates: list[dict] = []
    for list_id in LISTAS_FLUXO:
        for t in tasks_by_list.get(list_id, []):
            tags = tag_names(t)
            if TAG_EXECUTAR_TESTE in tags and TAG_TESTE_PROCESSADO not in tags:
                candidates.append(t)

    log.info("FLUXO 1: %d tarefa(s) a processar", len(candidates))

    for t1_summary in candidates:
        t1_id = t1_summary["id"]
        try:
            # get_task precisa aqui porque o summary de list_tasks nem sempre
            # traz linked_tasks/custom_fields completos
            t1 = cu.get_task(t1_id)
            create_test_pair(cu, t1, tag_ab_ids)
        except Exception as exc:  # noqa: BLE001
            log.exception("Falhou ao processar %s: %s", t1_id, exc)


def create_test_pair(cu: ClickUp, t1: dict, tag_ab_ids: set[str]) -> None:
    t1_id = t1["id"]
    t1_name = t1["name"]

    t1_status = (t1.get("status") or {}).get("status", "").lower()
    if (t1.get("status") or {}).get("type") == "closed":
        log.info("  T1 %s está em status fechado ('%s') — processando normalmente "
                 "(teste A/B contra conteúdo já publicado)", t1_id, t1_status)

    tipo_teste_id = dropdown_option_id(t1, CF_TIPO_TESTE)
    tipo_teste_label = dropdown_option_name(t1, CF_TIPO_TESTE)

    if tipo_teste_id is None:
        log.warning("T1 %s tem 'executar teste' mas 'Tipo de teste' está "
                    "vazio. Pulando (preencha o campo e rode de novo).", t1_id)
        return

    # RECUPERAÇÃO — se uma execução anterior já criou a T2 mas falhou
    # antes de marcar 'teste processado', não duplicar. Só marcar processada.
    if t1_already_has_variacao(t1, tag_ab_ids):
        log.warning("T1 %s já tem variação vinculada. Só marcando processada.",
                    t1_id)
        _mark_t1_processada(cu, t1_id)
        return

    t2_name = f"[TESTE A/B - {tipo_teste_label}] {t1_name}"
    t3_name = t2_name

    description = (
        f"Tarefa criada automaticamente a partir de "
        f"{t1.get('custom_id') or t1_id} — '{t1_name}'.\n\n"
        f"Tipo de teste: {tipo_teste_label}\n\n"
        f"{t1.get('text_content') or ''}"
    )

    # --- T2 em Planejamento ---
    # NOTA: tags no payload de create_task são inconsistentemente aplicadas
    # no ClickUp (mesmo bug que custom_fields). Criamos sem tag e adicionamos
    # via endpoint dedicado depois.
    t2 = cu.create_task(LIST_PLANEJAMENTO, {
        "name": t2_name,
        "description": description,
    })
    t2_id = t2["id"]
    log.info("  T2 criada: %s (%s)", t2_id, t2_name)
    try:
        cu.add_tag(t2_id, TAG_TESTE_AB)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao adicionar tag 'teste a/b' em T2 %s: %s",
                    t2_id, exc)
    apply_custom_fields(cu, t2_id, t1)

    # --- T3 em Testes A/B ---
    t3 = cu.create_task(LIST_TESTE_AB, {
        "name": t3_name,
        "description": description,
    })
    t3_id = t3["id"]
    log.info("  T3 criada: %s (%s)", t3_id, t3_name)
    apply_custom_fields(cu, t3_id, t1, extra_tipo_teste_id=tipo_teste_id)

    # --- Vínculos ---
    for a, b in [(t1_id, t2_id), (t2_id, t3_id), (t1_id, t3_id)]:
        try:
            cu.link_tasks(a, b)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao vincular %s ↔ %s: %s", a, b, exc)

    # --- Relacionamento T3 → T2 ---
    try:
        cu.set_custom_field(t3_id, CF_PLANEJAMENTO_REL, {"add": [t2_id]})
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao setar 'Planejamento' em T3 %s: %s", t3_id, exc)

    # --- Marca T1 como processada ---
    _mark_t1_processada(cu, t1_id)


def _mark_t1_processada(cu: ClickUp, t1_id: str) -> None:
    try:
        cu.add_tag(t1_id, TAG_TESTE_PROCESSADO)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao adicionar 'teste processado' em %s: %s",
                    t1_id, exc)
    try:
        cu.remove_tag(t1_id, TAG_EXECUTAR_TESTE)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao remover 'executar teste' de %s: %s", t1_id, exc)
    log.info("  T1 %s marcada como processada", t1_id)


# ---------------------------------------------------------------------------
# FLUXO 2 — sincroniza status/data da T3 com a T2
# ---------------------------------------------------------------------------

def process_status_sync(cu: ClickUp, tasks_by_list: dict[str, list[dict]],
                        testeab_ids: set[str],
                        testeab_by_id: dict[str, dict]) -> None:
    mapping = {
        LIST_PLANEJAMENTO: ("planejamento", STATUS_T3_PLANEJAMENTO),
        LIST_COPY: ("em_producao", STATUS_T3_EM_PRODUCAO),
        LIST_DESIGN: ("em_producao", STATUS_T3_EM_PRODUCAO),
        LIST_AGENDAMENTOS: ("agendado", STATUS_T3_ANALISE),
    }

    for list_id, (kind, target_status) in mapping.items():
        tasks = tasks_by_list.get(list_id, [])
        t2s = [t for t in tasks if TAG_TESTE_AB in tag_names(t)]
        if not t2s:
            continue
        log.info("FLUXO 2: lista %s → %d candidata(s)", list_id, len(t2s))

        for t2_summary in t2s:
            try:
                # get_task necessário pra obter linked_tasks e custom_fields
                t2 = cu.get_task(t2_summary["id"])
                _sync_one_t2(cu, t2, kind, target_status,
                             testeab_ids, testeab_by_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("Falha no sync de T2 %s: %s",
                            t2_summary["id"], exc)


def _sync_one_t2(cu: ClickUp, t2: dict, kind: str, target_status: str,
                 testeab_ids: set[str],
                 testeab_by_id: dict[str, dict]) -> None:
    t3_id = find_linked_t3_in_testeab(t2, testeab_ids)
    if not t3_id:
        # Não é T2 de verdade (tarefa com a tag mas sem T3 correspondente)
        return

    # Usa a T3 do cache (summary da list_tasks já traz status)
    t3 = testeab_by_id.get(t3_id) or cu.get_task(t3_id)
    current = (t3.get("status") or {}).get("status", "").lower()
    if current in STATUS_T3_TERMINAIS:
        log.info("  T3 %s em '%s' (terminal) — ignorando", t3_id, current)
        return

    payload: dict[str, Any] = {}
    if current != target_status:
        payload["status"] = target_status

    if kind == "agendado":
        data_postagem = cf_value(t2, CF_DATA_POSTAGEM)
        if data_postagem:
            try:
                payload["due_date"] = int(data_postagem)
                payload["due_date_time"] = False
            except (TypeError, ValueError):
                log.warning("Data Postagem de T2 %s inválida: %r",
                            t2["id"], data_postagem)

    if not payload:
        return

    try:
        cu.update_task(t3_id, payload)
        log.info("  T3 %s ← %s", t3_id, payload)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao atualizar T3 %s: %s", t3_id, exc)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        log.error("CLICKUP_API_TOKEN ausente.")
        return 1

    dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
    if dry_run:
        log.info("*** MODO DRY_RUN: nenhuma escrita será feita ***")

    cu = ClickUp(token, dry_run=dry_run)

    try:
        # Filtered search do ClickUp: só pega tarefas com tag relevante.
        # Muito mais rápido que paginar listas inteiras (Agendamentos tem 3k+).
        log.info("Pré-carregando tarefas com filtered search...")

        # FLUXO 1 candidatas: tag 'executar teste' nas 4 listas
        executar_tasks = cu.filter_team_tasks(
            list_ids=LISTAS_FLUXO, tags=[TAG_EXECUTAR_TESTE]
        )
        log.info("  Tarefas com 'executar teste': %d", len(executar_tasks))

        # FLUXO 2 candidatas: tag 'teste a/b' nas 4 listas
        ab_tasks = cu.filter_team_tasks(
            list_ids=LISTAS_FLUXO, tags=[TAG_TESTE_AB]
        )
        log.info("  Tarefas com 'teste a/b' no fluxo: %d", len(ab_tasks))

        # Tarefas da lista Testes A/B (pra saber quais IDs estão lá e cache de status)
        testeab_tasks = cu.list_tasks(LIST_TESTE_AB)
        testeab_ids = {t["id"] for t in testeab_tasks}
        testeab_by_id = {t["id"]: t for t in testeab_tasks}
        log.info("  Lista Testes A/B: %d tarefas", len(testeab_tasks))

        # Monta tasks_by_list só com as tarefas relevantes, agrupadas por lista
        tasks_by_list: dict[str, list[dict]] = {lid: [] for lid in LISTAS_FLUXO}
        seen_ids: set[str] = set()
        for t in executar_tasks + ab_tasks:
            if t["id"] in seen_ids:
                continue
            seen_ids.add(t["id"])
            lid = t.get("list", {}).get("id")
            if lid in tasks_by_list:
                tasks_by_list[lid].append(t)

        tag_ab_ids = {t["id"] for t in ab_tasks}

        process_executar_teste(cu, tasks_by_list, testeab_ids, tag_ab_ids)
        process_status_sync(cu, tasks_by_list, testeab_ids, testeab_by_id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erro fatal: %s", exc)
        return 1

    log.info("Sincronização concluída.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
