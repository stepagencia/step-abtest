"""
STEP - Sincronização automática de Testes A/B de Conteúdo
==========================================================

O que este script faz a cada execução:

FLUXO 1 — Detectar novos testes A/B
  Busca tarefas na lista "Planejamento de conteúdo N1" que tenham
  a etiqueta `executar teste` e AINDA NÃO foram processadas (sem a
  etiqueta `teste processado`).

  Para cada uma:
    1. Lê os custom fields (Cliente, Rede Social, Link do Post
       Original, Tipo, Editorias, Data da Postagem, Tipo de teste A/B)
       e a descrição.
    2. Cria a Tarefa 2 (nova tarefa de conteúdo) na mesma lista de
       Planejamento, com etiqueta `teste a/b`, copiando todos os
       campos e com o nome `[TESTE A/B - {tipo}] {nome original}`.
    3. Cria a Tarefa 3 (registro) na lista "Testes A/B de Conteúdo",
       copiando os mesmos campos + preenchendo "Tipo de teste" com
       o valor do dropdown da original. Também preenche o campo
       relacionamento "Planejamento" apontando para a Tarefa 2.
    4. Vincula as três tarefas entre si (link_task).
    5. Remove a etiqueta `executar teste` da Tarefa 1 e adiciona
       `teste processado` (idempotência).

FLUXO 2 — Sincronizar status e data da Tarefa 3
  Busca todas as Tarefas 2 (etiqueta `teste a/b`) e, para cada uma,
  encontra a Tarefa 3 vinculada (via linked_tasks).

  Regras:
    - Tarefa 2 em lista Copy Conteúdo OU Design/Edição
        → Tarefa 3 status "em produção"
    - Tarefa 2 em lista Agendamentos
        → Tarefa 3 status "análise"
        + Data de vencimento = Data da Postagem da Tarefa 2

Segurança
---------
- Usa ClickUp API token via env var CLICKUP_API_TOKEN
- Não apaga nada: só move, atualiza campos e troca etiquetas
- Idempotente: roda de 5 em 5 min sem duplicar tarefas

Como rodar localmente:
    export CLICKUP_API_TOKEN="pk_..."
    python ab_test_sync.py

Em produção: GitHub Actions (ver .github/workflows/sync.yml)
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Configuração — IDs do workspace da STEP
# ---------------------------------------------------------------------------

WORKSPACE_ID = "9013038195"

# Listas
LIST_PLANEJAMENTO = "901306281641"       # Planejamento de conteúdo N1
LIST_COPY = "901306281633"               # Copy conteúdo
LIST_DESIGN = "901306281639"             # Design/Edição
LIST_AGENDAMENTOS = "901306281642"       # Agendamentos
LIST_TESTE_AB = "901326648620"           # Testes A/B de Conteúdo

# Etiquetas (tags)
TAG_EXECUTAR_TESTE = "executar teste"
TAG_TESTE_PROCESSADO = "teste processado"
TAG_TESTE_AB = "teste a/b"

# Custom field IDs (descobertos via MCP — são globais no workspace)
CF_CLIENTE = "e41a916f-7818-44b6-9e93-fb003f52ad53"
CF_REDE_SOCIAL = "5293fb4f-2741-4aab-bb1c-518e9e1d2030"
CF_TIPO = "4fc73c67-8c6e-4e73-ad8e-885df6586260"
CF_EDITORIAS = "7a155e2e-5b70-467c-894f-98f7f4cc1722"
CF_LINK_POST_ORIGINAL = "3ee94567-b2f1-4819-91f9-726fcb4378c0"
CF_DATA_POSTAGEM = "4ccefbf6-8e46-48af-8f94-1f3eeb8770f6"
CF_LEGENDA = "322837ee-3eba-41a8-8a5e-82b61fa15366"
CF_PLANEJAMENTO_REL = "5addfdcc-5182-4547-9d3a-89bd31094118"
CF_TIPO_TESTE = "273dcb9f-81ee-49bc-b0ec-9ef169bccceb"  # existe na lista Teste A/B; precisa ser adicionado em Planejamento também

# Campos a copiar integralmente da Tarefa 1 → Tarefa 2 → Tarefa 3
COPIABLE_FIELDS = [
    CF_CLIENTE,
    CF_REDE_SOCIAL,
    CF_TIPO,
    CF_EDITORIAS,
    CF_LINK_POST_ORIGINAL,
    CF_DATA_POSTAGEM,
    CF_LEGENDA,
]

# Status da lista Testes A/B
# NOTA: se sua lista tiver nomes diferentes, edite aqui. Os status vistos
# em tarefas reais foram "adicionado ao planejamento" e "análise".
STATUS_TESTEAB_EM_PRODUCAO = "em produção"
STATUS_TESTEAB_ANALISE = "análise"

# Listas que sinalizam "em produção" na Tarefa 3
LISTAS_EM_PRODUCAO = {LIST_COPY, LIST_DESIGN}
LISTA_AGENDADO = LIST_AGENDAMENTOS

# ---------------------------------------------------------------------------
# Cliente ClickUp
# ---------------------------------------------------------------------------

log = logging.getLogger("ab_test_sync")

API_BASE = "https://api.clickup.com/api/v2"


class ClickUp:
    """Wrapper fininho sobre a API do ClickUp com retry simples."""

    def __init__(self, token: str) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
        })

    def _req(self, method: str, path: str, **kwargs: Any) -> Any:
        url = f"{API_BASE}{path}"
        for attempt in range(3):
            resp = self.session.request(method, url, timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limit, aguardando %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log.error("ClickUp API %s %s -> %s: %s",
                          method, path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
            if resp.text:
                return resp.json()
            return None
        resp.raise_for_status()

    # ----- Leitura -----

    def list_tasks(self, list_id: str, **params: Any) -> list[dict]:
        """Pagina todas as tarefas de uma lista."""
        out: list[dict] = []
        page = 0
        while True:
            data = self._req("GET", f"/list/{list_id}/task",
                             params={**params, "page": page, "subtasks": "true"})
            tasks = data.get("tasks", [])
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
        return self._req("POST", f"/list/{list_id}/task", json=payload)

    def update_task(self, task_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/task/{task_id}", json=payload)

    def set_custom_field(self, task_id: str, field_id: str, value: Any) -> None:
        self._req("POST", f"/task/{task_id}/field/{field_id}",
                  json={"value": value})

    def add_tag(self, task_id: str, tag: str) -> None:
        self._req("POST", f"/task/{task_id}/tag/{tag}")

    def remove_tag(self, task_id: str, tag: str) -> None:
        self._req("DELETE", f"/task/{task_id}/tag/{tag}")

    def link_tasks(self, task_id: str, links_to: str) -> None:
        self._req("POST", f"/task/{task_id}/link/{links_to}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cf_value(task: dict, field_id: str) -> Any:
    """Extrai o valor de um custom field da resposta do ClickUp."""
    for cf in task.get("custom_fields", []):
        if cf["id"] == field_id:
            return cf.get("value")
    return None


def dropdown_option_id(task: dict, field_id: str) -> Optional[str]:
    """
    Para dropdowns, a API retorna o índice da opção em 'value' (int) e
    a lista de opções em type_config.options. Queremos o orderindex/id
    da opção selecionada.
    """
    for cf in task.get("custom_fields", []):
        if cf["id"] != field_id:
            continue
        val = cf.get("value")
        if val is None:
            return None
        # Em dropdowns o 'value' já é o orderindex (int) ou o UUID da opção
        options = cf.get("type_config", {}).get("options", [])
        if isinstance(val, int):
            if 0 <= val < len(options):
                return options[val]["id"]
            return None
        if isinstance(val, str):
            # Já é o UUID da opção
            return val
    return None


def build_custom_fields_payload(source_task: dict,
                                extra_tipo_teste_id: Optional[str] = None
                                ) -> list[dict]:
    """
    Monta o array de custom_fields para enviar no create_task, copiando
    todos os CAMPOS COPIÁVEIS da tarefa original.
    Se extra_tipo_teste_id for dado, adiciona também o campo "Tipo de teste".
    """
    out: list[dict] = []
    for field_id in COPIABLE_FIELDS:
        raw = cf_value(source_task, field_id)
        if raw is None or raw == "":
            continue
        # Dropdowns: precisamos passar o UUID da opção, não o índice
        if field_id in {CF_CLIENTE, CF_REDE_SOCIAL, CF_TIPO, CF_EDITORIAS}:
            opt_id = dropdown_option_id(source_task, field_id)
            if opt_id:
                out.append({"id": field_id, "value": opt_id})
        else:
            out.append({"id": field_id, "value": raw})

    if extra_tipo_teste_id:
        out.append({"id": CF_TIPO_TESTE, "value": extra_tipo_teste_id})

    return out


def tag_names(task: dict) -> set[str]:
    return {t["name"] for t in task.get("tags", [])}


# ---------------------------------------------------------------------------
# FLUXO 1 — criar Tarefas 2 e 3 a partir da Tarefa 1
# ---------------------------------------------------------------------------

def process_executar_teste(cu: ClickUp) -> None:
    """Encontra tarefas com 'executar teste' e ainda não processadas."""
    tasks = cu.list_tasks(LIST_PLANEJAMENTO)
    candidates = [
        t for t in tasks
        if TAG_EXECUTAR_TESTE in tag_names(t)
        and TAG_TESTE_PROCESSADO not in tag_names(t)
    ]
    log.info("FLUXO 1: %d tarefa(s) a processar", len(candidates))

    for t1_summary in candidates:
        t1_id = t1_summary["id"]
        try:
            t1 = cu.get_task(t1_id)
            create_test_pair(cu, t1)
        except Exception as exc:  # noqa: BLE001
            log.exception("Falhou ao processar tarefa %s: %s", t1_id, exc)


def create_test_pair(cu: ClickUp, t1: dict) -> None:
    """Cria Tarefa 2 (nova de conteúdo) e Tarefa 3 (registro)."""
    t1_id = t1["id"]
    t1_name = t1["name"]

    # Qual é o tipo de teste escolhido?
    tipo_teste_id = dropdown_option_id(t1, CF_TIPO_TESTE)
    tipo_teste_label = None
    for cf in t1.get("custom_fields", []):
        if cf["id"] == CF_TIPO_TESTE and tipo_teste_id:
            for opt in cf.get("type_config", {}).get("options", []):
                if opt["id"] == tipo_teste_id:
                    tipo_teste_label = opt["name"]

    if not tipo_teste_id:
        log.warning("Tarefa %s tem etiqueta 'executar teste' mas não preencheu "
                    "o campo 'Tipo de teste A/B'. Pulando.", t1_id)
        return

    label_suffix = f" - {tipo_teste_label}" if tipo_teste_label else ""
    t2_name = f"[TESTE A/B{label_suffix}] {t1_name}"
    t3_name = t2_name  # mesmo nome na lista de Teste A/B

    description = (f"Tarefa criada automaticamente a partir da tarefa "
                   f"{t1.get('custom_id', t1_id)} — '{t1_name}'.\n\n"
                   f"Tipo de teste: {tipo_teste_label or '?'}\n\n"
                   f"{t1.get('text_content') or ''}")

    # --- Cria Tarefa 2 (na lista Planejamento, com tag 'teste a/b') ---
    t2_payload = {
        "name": t2_name,
        "description": description,
        "tags": [TAG_TESTE_AB],
        "custom_fields": build_custom_fields_payload(t1),
    }
    t2 = cu.create_task(LIST_PLANEJAMENTO, t2_payload)
    t2_id = t2["id"]
    log.info("  Tarefa 2 criada: %s (%s)", t2_id, t2_name)

    # --- Cria Tarefa 3 (na lista Testes A/B) ---
    t3_payload = {
        "name": t3_name,
        "description": description,
        "custom_fields": build_custom_fields_payload(
            t1, extra_tipo_teste_id=tipo_teste_id
        ),
    }
    t3 = cu.create_task(LIST_TESTE_AB, t3_payload)
    t3_id = t3["id"]
    log.info("  Tarefa 3 criada: %s (%s)", t3_id, t3_name)

    # --- Vincula as três (link bidirecional via /link/{id}) ---
    try:
        cu.link_tasks(t1_id, t2_id)
        cu.link_tasks(t2_id, t3_id)
        cu.link_tasks(t1_id, t3_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao vincular tarefas: %s", exc)

    # --- Preenche o campo relacionamento 'Planejamento' na Tarefa 3
    # apontando para a Tarefa 2 (é um campo tipo 'tasks')
    try:
        cu.set_custom_field(t3_id, CF_PLANEJAMENTO_REL,
                            {"add": [t2_id]})
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao preencher campo relacionamento: %s", exc)

    # --- Marca Tarefa 1 como processada ---
    cu.add_tag(t1_id, TAG_TESTE_PROCESSADO)
    try:
        cu.remove_tag(t1_id, TAG_EXECUTAR_TESTE)
    except Exception as exc:  # noqa: BLE001
        log.warning("Falha ao remover tag 'executar teste' de %s: %s",
                    t1_id, exc)

    log.info("  Tarefa 1 %s marcada como processada", t1_id)


# ---------------------------------------------------------------------------
# FLUXO 2 — sincronizar status/data da Tarefa 3 com a Tarefa 2
# ---------------------------------------------------------------------------

@dataclass
class T2State:
    task_id: str
    list_id: str
    data_postagem: Optional[int]  # unix ms
    linked_t3_id: Optional[str]


def find_t3_for_t2(cu: "ClickUp", t2: dict) -> Optional[str]:
    """Encontra a Tarefa 3 vinculada via linked_tasks, mas SÓ retorna se
    a candidata estiver de fato na lista Testes A/B. Isso evita confundir
    tarefas com etiqueta 'teste a/b' que não são Tarefas 2 de verdade
    (tarefas-pai de planejamento, etc.) com testes reais.
    """
    for link in t2.get("linked_tasks", []):
        candidate_id = link.get("task_id") or link.get("link_id")
        if not candidate_id or candidate_id == t2["id"]:
            continue
        # Busca a candidata pra confirmar que está na lista Testes A/B
        try:
            candidate = cu.get_task(candidate_id)
            candidate_list = candidate.get("list", {}).get("id")
            if candidate_list == LIST_TESTE_AB:
                return candidate_id
        except Exception:  # noqa: BLE001
            continue
    return None


def process_status_sync(cu: ClickUp) -> None:
    """Varre Tarefas 2 (etiqueta 'teste a/b') em todas as listas do fluxo
    de conteúdo e alinha a Tarefa 3 correspondente.
    """
    for list_id, kind in [
        (LIST_PLANEJAMENTO, "planejamento"),
        (LIST_COPY, "em_producao"),
        (LIST_DESIGN, "em_producao"),
        (LIST_AGENDAMENTOS, "agendado"),
    ]:
        tasks = cu.list_tasks(list_id)
        t2s = [t for t in tasks if TAG_TESTE_AB in tag_names(t)]
        if not t2s:
            continue
        log.info("FLUXO 2: lista %s → %d candidata(s) com tag 'teste a/b'",
                 list_id, len(t2s))

        for t2_summary in t2s:
            t2 = cu.get_task(t2_summary["id"])
            t3_id = find_t3_for_t2(cu, t2)
            if not t3_id:
                # Sem T3 na lista Testes A/B = não é Tarefa 2 de verdade,
                # é só uma tarefa com a etiqueta por outro motivo. Pula em silêncio.
                continue
            data_postagem = cf_value(t2, CF_DATA_POSTAGEM)

            if kind == "em_producao":
                try:
                    cu.update_task(t3_id,
                                   {"status": STATUS_TESTEAB_EM_PRODUCAO})
                    log.info("  T3 %s → em produção", t3_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Não consegui mover T3 %s para em produção: %s",
                                t3_id, exc)

            elif kind == "agendado":
                payload: dict[str, Any] = {"status": STATUS_TESTEAB_ANALISE}
                if data_postagem:
                    payload["due_date"] = int(data_postagem)
                    payload["due_date_time"] = False
                try:
                    cu.update_task(t3_id, payload)
                    log.info("  T3 %s → análise (due=%s)", t3_id, data_postagem)
                except Exception as exc:  # noqa: BLE001
                    log.warning("Não consegui atualizar T3 %s: %s", t3_id, exc)


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
        log.error("Variável CLICKUP_API_TOKEN ausente.")
        return 1

    cu = ClickUp(token)

    try:
        process_executar_teste(cu)
        process_status_sync(cu)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erro fatal: %s", exc)
        return 1

    log.info("Sincronização concluída.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
