#!/usr/bin/env python3
"""
novo_cliente_criar_tarefas.py
Automação Step — Cria reuniões e planejamentos para novos especialistas.
Roda uma vez por dia via GitHub Actions.

GRS / GRS + TRÁFEGO  → Reunião de Input + Gravação de Conteúdo (mensais)
                       + Reunião de resultados trimestral (mar/jun/set/dez)
Direção Estratégica  → Reunião de resultados mensal
Todos os planos      → Planejamento de Conteúdo mensal

DRY_RUN=true → só loga o que faria, sem escrever no ClickUp.
LIMIT=N     → processa só os N primeiros especialistas (teste).
"""

import os
import re
import requests
from datetime import date, datetime

API_TOKEN = os.environ["CLICKUP_API_TOKEN"]
HEADERS   = {"Authorization": API_TOKEN, "Content-Type": "application/json"}
BASE      = "https://api.clickup.com/api/v2"

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
try:
    LIMIT = int(os.environ.get("LIMIT", "").strip())
except (ValueError, TypeError):
    LIMIT = None

LIST_GESTAO   = "901301376959"
LIST_REUNIOES = "901305872401"
LIST_PLAN     = os.environ.get("CLICKUP_PLANEJAMENTO_LIST_ID", "")
LIST_PGM = "901305920069"

FIELD_PLANO = "f815815a-7ea4-468c-b956-318bb999d492"
FIELD_CICLO = "0ee5bf2a-f32d-4498-91f9-905aa2a0faf1"

STATUS_REUNIOES = "Próximas Reuniões"
STATUS_PLAN     = "Próximos Planejamentos"
VALID_STATUSES  = {"ativo", "em fase de onboarding"}

# ─────────────────────────────────────────────────────────────────────────────
# Statuses das listas destino — DESCOBERTOS via API no início do main().
# Motivo: descoberto empiricamente que GET /list/{id}/task e
# GET /team/{id}/task com order_by=created omitem tarefas silenciosamente.
# Único jeito confiável de pegar TUDO é iterar por status.
# Descobrir dinamicamente evita bug "esqueci de adicionar status novo".
# ─────────────────────────────────────────────────────────────────────────────

# Prefixos que identificam o TIPO de cada tarefa gerada pelo script — combinado
# com Cliente + CICLO, forma uma chave de dedupe resistente a renomeação.
TIPO_PREFIXOS = {
    "reuniao_input": "Reunião de Input",
    "gravacao": "Gravação de Conteúdo",
    "reuniao_resultados": "Reunião de resultados",
    "planejamento": "[Planejamento de Conteúdo]",
    "pgm": "PGM #",
}

MESES = {
    1:"Janeiro", 2:"Fevereiro", 3:"Março", 4:"Abril",
    5:"Maio", 6:"Junho", 7:"Julho", 8:"Agosto",
    9:"Setembro", 10:"Outubro", 11:"Novembro", 12:"Dezembro"
}
QUARTER_END = {3:"1º", 6:"2º", 9:"3º", 12:"4º"}
QUARTER_DATES = {
    1: ("01/01", "31/03"),
    2: ("01/04", "30/06"),
    3: ("01/07", "30/09"),
    4: ("01/10", "31/12"),
}
QUARTER_START_MONTH = {1: 1, 2: 4, 3: 7, 4: 10}
TEAM_ID = "9013038195"


def api_get(path, params=None):
    r = requests.get(f"{BASE}{path}", headers=HEADERS, params=params or {})
    r.raise_for_status()
    return r.json()

def api_get_qs(path, params_list):
    """GET aceitando lista de tuplas (pra params com [] repetidos)."""
    import urllib.parse
    q = urllib.parse.urlencode(params_list, doseq=True)
    r = requests.get(f"{BASE}{path}?{q}", headers=HEADERS)
    r.raise_for_status()
    return r.json()

def api_post(path, payload):
    if DRY_RUN:
        print(f"      [DRY_RUN] POST {path} {payload}")
        return {"id": "DRYRUN_ID", "name": payload.get("name", "?")}
    r = requests.post(f"{BASE}{path}", headers=HEADERS, json=payload)
    if r.status_code not in (200, 201):
        print(f"      ✗ HTTP {r.status_code}: {r.text[:300]}")
        return None
    return r.json()


def discover_statuses(list_id):
    """Retorna a lista de nomes de status configurados na lista."""
    d = api_get(f"/list/{list_id}")
    return [s.get("status") for s in (d.get("statuses") or []) if s.get("status")]


def get_all_tasks(list_id, statuses=None):
    """Pagina TODAS as tarefas de uma lista, incluindo arquivadas.

    IMPORTANTE — motivo dessa complexidade:
      GET /list/{id}/task e GET /team/{id}/task com order_by=created omitem
      tarefas silenciosamente (bug/limitação do ClickUp). Testado e reproduzido
      em 2026-07-23: dos 451 planejamentos existentes, os endpoints retornam
      só 387; até tarefas recém-criadas pelo próprio script somem.

      Único jeito confiável: paginar POR STATUS via filtered team search.
      Também roda 2ª passada com archived=true pra pegar tarefas arquivadas
      (dedupe contra elas evita recriar visualmente uma que foi arquivada).

    Se statuses=None, descobre dinamicamente via discover_statuses(list_id).
    """
    if statuses is None:
        statuses = discover_statuses(list_id)

    if not statuses:
        # Ultra-fallback: modo antigo (só se lista sem statuses configurados)
        all_tasks, page = [], 0
        while True:
            data = api_get(f"/list/{list_id}/task",
                           {"page": page, "include_closed": "true",
                            "archived": "false"})
            batch = data.get("tasks", [])
            all_tasks.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return all_tasks

    seen = {}
    for archived_flag in ("false", "true"):
        for status in statuses:
            page = 0
            while True:
                params = [
                    ("list_ids[]", list_id),
                    ("include_closed", "true"),
                    ("archived", archived_flag),
                    ("subtasks", "true"),
                    ("statuses[]", status),
                    ("page", page),
                ]
                data = api_get_qs(f"/team/{TEAM_ID}/task", params)
                batch = data.get("tasks", [])
                for t in batch:
                    seen[t["id"]] = t
                if len(batch) < 100:
                    break
                page += 1
    return list(seen.values())


def infer_tipo_from_name(name):
    """Retorna a chave em TIPO_PREFIXOS que casa com o nome, ou None."""
    n = (name or "").strip()
    for tipo, prefixo in TIPO_PREFIXOS.items():
        if n.startswith(prefixo):
            return tipo
    return None


def get_cf_option_id(task, field_id):
    """UUID da opção selecionada num dropdown na task. Aceita val como
    orderindex (int) ou uuid (str). None se vazio."""
    for cf in task.get("custom_fields", []):
        if cf["id"] != field_id:
            continue
        val = cf.get("value")
        if val is None or val == "":
            return None
        opts = cf.get("type_config", {}).get("options", [])
        if isinstance(val, int) and 0 <= val < len(opts):
            return opts[val].get("id")
        if isinstance(val, str):
            return val
    return None


def build_dedupe_index(tasks, cf_cliente_id, cf_ciclo_id):
    """Retorna set de (cliente_opt_uuid, ciclo_opt_uuid, tipo) — dedupe
    robusto que resiste a renomeação manual da tarefa."""
    idx = set()
    if not cf_cliente_id or not cf_ciclo_id:
        return idx
    for t in tasks:
        cliente_opt = get_cf_option_id(t, cf_cliente_id)
        ciclo_opt = get_cf_option_id(t, cf_ciclo_id)
        tipo = infer_tipo_from_name(t.get("name") or "")
        if cliente_opt and ciclo_opt and tipo:
            idx.add((cliente_opt, ciclo_opt, tipo))
    return idx


def build_dedupe_pgm_index(tasks, cf_cliente_id):
    """PGM não tem CICLO (usa quarter/year no nome). Retorna set de
    (cliente_opt_uuid, quarter, year)."""
    idx = set()
    if not cf_cliente_id:
        return idx
    pat = re.compile(r"^PGM #0(\d+) \[(\d{4})\]")
    for t in tasks:
        m = pat.match(t.get("name") or "")
        if not m:
            continue
        cliente_opt = get_cf_option_id(t, cf_cliente_id)
        if cliente_opt:
            idx.add((cliente_opt, int(m.group(1)), int(m.group(2))))
    return idx

def get_specialist_tasks():
    """Busca especialistas ativos individualmente para garantir custom fields completos."""
    list_tasks = get_all_tasks(LIST_GESTAO)
    active = [t for t in list_tasks
              if t.get("status", {}).get("status", "").lower() in VALID_STATUSES]
    result = []
    for t in active:
        full = api_get(f"/task/{t['id']}")
        result.append(full)
    return result

def build_field_index(list_id):
    fields = api_get(f"/list/{list_id}/field").get("fields", [])
    index  = {}
    for f in fields:
        opts = {}
        for i, o in enumerate(f.get("type_config", {}).get("options", [])):
            # normaliza orderindex para int para evitar mismatch 6 vs 6.0
            try:
                oi = int(float(str(o.get("orderindex", i))))
            except (ValueError, TypeError):
                oi = i
            opts[o.get("name", "").upper()] = {
                "id": o.get("id", ""),
                "orderindex": oi
            }
        index[f["name"].lower()] = {"id": f["id"], "options": opts}
    return index

def get_cf_raw(task, field_id):
    for cf in task.get("custom_fields", []):
        if cf["id"] == field_id:
            return cf.get("value")
    return None

def to_ms(year, month, day):
    return int(datetime.combine(date(year, month, day),
                                datetime.min.time()).timestamp() * 1000)

def get_plano_options():
    """Busca as opções do campo Plano diretamente pelo ID do campo."""
    fields = api_get(f"/list/{LIST_GESTAO}/field").get("fields", [])
    for f in fields:
        if f["id"] == FIELD_PLANO:
            result = {}
            for o in f.get("type_config", {}).get("options", []):
                try:
                    oi = str(int(float(str(o.get("orderindex", 0)))))
                except Exception:
                    oi = str(o.get("orderindex", 0))
                nome = o.get("name", "").upper()
                result[oi] = nome
                result[str(o.get("id", ""))] = nome
            print(f"Plano options carregadas: {list(result.items())[:8]}")
            return result
    print("AVISO: campo Plano não encontrado")
    return {}


def resolve_plano(task, plano_opts_by_value):
    raw = get_cf_raw(task, FIELD_PLANO)
    if raw is None:
        return None
    # normaliza raw para int string: 6, 6.0, "6", "6.0" → "6"
    try:
        key = str(int(float(str(raw))))
    except (ValueError, TypeError):
        key = str(raw)
    label = plano_opts_by_value.get(key)
    return label.upper() if label else None

def resolve_ciclo_orderindex(opts_dict, month_num):
    key = MESES[month_num].upper()
    opt = opts_dict.get(key)
    return opt["orderindex"] if opt else None

def resolve_cliente_orderindex(opts_dict, specialist_name):
    name_upper = specialist_name.strip().upper()
    if name_upper in opts_dict:
        return opts_dict[name_upper]["orderindex"]
    for opt_name, opt_data in opts_dict.items():
        if name_upper in opt_name or opt_name in name_upper:
            return opt_data["orderindex"]
    return None


def resolve_cliente_uuid(opts_dict, specialist_name):
    """Igual ao resolve_cliente_orderindex mas retorna UUID (pra dedupe)."""
    name_upper = specialist_name.strip().upper()
    if name_upper in opts_dict:
        return opts_dict[name_upper].get("id")
    for opt_name, opt_data in opts_dict.items():
        if name_upper in opt_name or opt_name in name_upper:
            return opt_data.get("id")
    return None


def resolve_ciclo_uuid(opts_dict, month_num):
    """UUID da opção CICLO pro mês. Pra dedupe."""
    if not month_num:
        return None
    key = MESES[month_num].upper()
    opt = opts_dict.get(key)
    return opt.get("id") if opt else None


def process_specialist(task, today, year, plano_opts_by_value,
                       fields_reu, fields_plan, fields_pgm,
                       existing_reu, existing_plan, existing_pgm,
                       dedupe_reu, dedupe_plan, dedupe_pgm, task_types):
    nome  = task["name"]
    plano = resolve_plano(task, plano_opts_by_value)

    if not plano:
        raw = get_cf_raw(task, FIELD_PLANO)
        print(f"   → {nome}: plano não resolvido (raw={raw}), pulando")
        return

    print(f"\n  ▸ {nome}  |  {plano}")

    ciclo_field_reu   = fields_reu.get("ciclo", {}).get("id")
    ciclo_opts_reu    = fields_reu.get("ciclo", {}).get("options", {})
    cliente_field_reu = fields_reu.get("cliente", {}).get("id")
    cliente_opts_reu  = fields_reu.get("cliente", {}).get("options", {})

    ciclo_field_plan   = fields_plan.get("ciclo", {}).get("id") if LIST_PLAN else None
    ciclo_opts_plan    = fields_plan.get("ciclo", {}).get("options", {}) if LIST_PLAN else {}
    cliente_field_plan = fields_plan.get("cliente", {}).get("id") if LIST_PLAN else None
    cliente_opts_plan  = fields_plan.get("cliente", {}).get("options", {}) if LIST_PLAN else {}

    cliente_field_pgm  = fields_pgm.get("cliente", {}).get("id") if LIST_PGM else None
    cliente_opts_pgm   = fields_pgm.get("cliente", {}).get("options", {}) if LIST_PGM else {}

    months   = list(range(today.month, 13))
    quarters = [q for q in QUARTER_END if q >= today.month]
    is_grs   = "GRS" in plano
    is_dir   = "DIRE" in plano
    TYPE_REUNIAO      = task_types["REUNIÃO"]
    TYPE_PLANEJAMENTO = task_types["PLANEJAMENTO"]
    TYPE_GRAVACAO     = task_types["GRAVAÇÃO"]
    created  = 0

    def new_reuniao(name, month=None, due_ms=None, task_type_override=None):
        nonlocal created
        name = name.strip()
        # Dedupe 1: por nome exato
        if name in existing_reu:
            return
        # Dedupe 2: por (Cliente uuid, CICLO uuid, tipo) — resistente a renomeação
        tipo = infer_tipo_from_name(name)
        cliente_uuid = resolve_cliente_uuid(cliente_opts_reu, nome)
        ciclo_uuid = resolve_ciclo_uuid(ciclo_opts_reu, month)
        dedupe_key = None
        if tipo and cliente_uuid and ciclo_uuid:
            dedupe_key = (cliente_uuid, ciclo_uuid, tipo)
            if dedupe_key in dedupe_reu:
                print(f"      ⊘ dedupe (Cliente+CICLO+tipo): {name}")
                return
        cfs = []
        if ciclo_field_reu and month:
            idx = resolve_ciclo_orderindex(ciclo_opts_reu, month)
            if idx is not None:
                cfs.append({"id": ciclo_field_reu, "value": idx})
        if cliente_field_reu:
            idx = resolve_cliente_orderindex(cliente_opts_reu, nome)
            if idx is not None:
                cfs.append({"id": cliente_field_reu, "value": idx})
        payload = {"name": name, "status": STATUS_REUNIOES}
        payload["custom_item_id"] = task_type_override if task_type_override else TYPE_REUNIAO
        if cfs:
            payload["custom_fields"] = cfs
        if due_ms:
            payload["due_date"] = due_ms
        result = api_post(f"/list/{LIST_REUNIOES}/task", payload)
        if result:
            existing_reu.add(name)
            if dedupe_key:
                dedupe_reu.add(dedupe_key)
            created += 1
            print(f"      ✓ {name}")

    def new_plan(name, month, due_ms):
        nonlocal created
        if not LIST_PLAN:
            return
        name = name.strip()
        if name in existing_plan:
            return
        tipo = infer_tipo_from_name(name)
        cliente_uuid = resolve_cliente_uuid(cliente_opts_plan, nome)
        ciclo_uuid = resolve_ciclo_uuid(ciclo_opts_plan, month)
        dedupe_key = None
        if tipo and cliente_uuid and ciclo_uuid:
            dedupe_key = (cliente_uuid, ciclo_uuid, tipo)
            if dedupe_key in dedupe_plan:
                print(f"      ⊘ dedupe (Cliente+CICLO+tipo): {name}")
                return
        cfs = []
        if ciclo_field_plan and month:
            idx = resolve_ciclo_orderindex(ciclo_opts_plan, month)
            if idx is not None:
                cfs.append({"id": ciclo_field_plan, "value": idx})
        if cliente_field_plan:
            idx = resolve_cliente_orderindex(cliente_opts_plan, nome)
            if idx is not None:
                cfs.append({"id": cliente_field_plan, "value": idx})
        payload = {"name": name, "status": STATUS_PLAN, "due_date": due_ms}
        payload["custom_item_id"] = TYPE_PLANEJAMENTO
        if cfs:
            payload["custom_fields"] = cfs
        result = api_post(f"/list/{LIST_PLAN}/task", payload)
        if result:
            existing_plan.add(name)
            if dedupe_key:
                dedupe_plan.add(dedupe_key)
            created += 1
            print(f"      ✓ {name}")

    def new_pgm(name, quarter_num, year):
        nonlocal created
        if not LIST_PGM:
            return
        name = name.strip()
        if name in existing_pgm:
            return
        # Dedupe 2 PGM: (Cliente, quarter, year) — não tem CICLO
        cliente_uuid = resolve_cliente_uuid(cliente_opts_pgm, nome)
        dedupe_key = (cliente_uuid, quarter_num, year) if cliente_uuid else None
        if dedupe_key and dedupe_key in dedupe_pgm:
            print(f"      ⊘ dedupe (Cliente+quarter+ano): {name}")
            return

        start_str, end_str = QUARTER_DATES[quarter_num]
        start_ms = to_ms(year, QUARTER_START_MONTH[quarter_num], 1)
        end_month = [3, 6, 9, 12][quarter_num - 1]
        end_day   = [31, 30, 30, 31][quarter_num - 1]
        end_ms    = to_ms(year, end_month, end_day)

        cfs = []
        if cliente_field_pgm:
            idx = resolve_cliente_orderindex(cliente_opts_pgm, nome)
            if idx is not None:
                cfs.append({"id": cliente_field_pgm, "value": idx})

        payload = {
            "name":        name,
            "status":      "to do",
            "custom_type": TYPE_PLANEJAMENTO,
            "start_date":  start_ms,
            "due_date":    end_ms,
        }
        if cfs:
            payload["custom_fields"] = cfs

        result = api_post(f"/list/{LIST_PGM}/task", payload)
        if result:
            existing_pgm.add(name)
            if dedupe_key:
                dedupe_pgm.add(dedupe_key)
            created += 1
            print(f"      ✓ {name}")

    if is_grs:
        for m in months:
            label = f"{MESES[m]}/{year}"
            new_reuniao(f"Reunião de Input [{nome}] [{label}]", month=m)
            new_reuniao(f"Gravação de Conteúdo [{nome}] [{label}]", month=m, task_type_override=TYPE_GRAVACAO)
        for q in quarters:
            new_reuniao(f"Reunião de resultados {QUARTER_END[q]} trim {year} [{nome}]",
                        month=q, due_ms=to_ms(year, q, 15))

    if is_dir:
        for m in months:
            label = f"{MESES[m]}/{year}"
            m_due = m - 1 if m > 1 else 12
            y_due = year if m > 1 else year - 1
            new_reuniao(f"Reunião de resultados [{nome}] [{label}]",
                        month=m, due_ms=to_ms(y_due, m_due, 15))

    for m in months:
        label = f"{MESES[m]}/{year}"
        m_due = m - 1 if m > 1 else 12
        y_due = year if m > 1 else year - 1
        new_plan(f"[Planejamento de Conteúdo] [{nome}] [{label}]",
                 month=m, due_ms=to_ms(y_due, m_due, 10))

    # ── PGM (todos os planos) ─────────────────────────────────────────────────
    quarters_pgm = [q for q in range(1, 5) if QUARTER_START_MONTH[q] + 2 >= today.month]
    for q in quarters_pgm:
        start_str, end_str = QUARTER_DATES[q]
        ordinal = ["1trim", "2trim", "3trim", "4trim"][q - 1]
        num = f"0{q}"
        name_pgm = f"PGM #{num} [{year}] [{ordinal}] - [{nome}] [{start_str} à {end_str}]"
        new_pgm(name_pgm, q, year)

    print(f"      → {created} tarefas criadas")


def main():
    today = date.today()
    year  = today.year
    print(f"━━━ Automação Step [{today}] ━━━\n")

    if DRY_RUN:
        print("*** MODO DRY_RUN: nenhuma escrita será feita ***\n")
    if LIMIT is not None:
        print(f"*** LIMIT={LIMIT}: processando só os {LIMIT} primeiros especialistas ***\n")

    if not LIST_PLAN:
        print("⚠  CLICKUP_PLANEJAMENTO_LIST_ID não configurado\n")

    print("Carregando tarefas (paginação por status + incluindo arquivadas)...")
    specialists = get_specialist_tasks()
    tasks_reu = get_all_tasks(LIST_REUNIOES)
    tasks_plan = get_all_tasks(LIST_PLAN) if LIST_PLAN else []
    tasks_pgm = get_all_tasks(LIST_PGM) if LIST_PGM else []
    existing_reu = {t["name"].strip() for t in tasks_reu}
    existing_plan = {t["name"].strip() for t in tasks_plan}
    existing_pgm = {t["name"].strip() for t in tasks_pgm}

    print("Carregando campos...")
    # custom_item_id descobertos via diagnóstico:
    # 1005=Reunião, 1020=Gravação, 1002=Planejamento.
    # O ClickUp espera o ID NUMÉRICO nesse campo, não o nome.
    task_types = {
        "REUNIÃO":      1005,
        "PLANEJAMENTO": 1002,
        "GRAVAÇÃO":     1020,
    }
    fields_gestao = build_field_index(LIST_GESTAO)
    fields_reu    = build_field_index(LIST_REUNIOES)
    fields_plan   = build_field_index(LIST_PLAN) if LIST_PLAN else {}
    fields_pgm = build_field_index(LIST_PGM) if LIST_PGM else {}

    plano_opts_by_value = get_plano_options()

    # Índices de dedupe secundário (Cliente + CICLO + tipo) — protege contra
    # renomeação manual e contra tarefas que o get_all_tasks perde.
    cf_cli_reu = fields_reu.get("cliente", {}).get("id")
    cf_ciclo_reu = fields_reu.get("ciclo", {}).get("id")
    cf_cli_plan = fields_plan.get("cliente", {}).get("id") if LIST_PLAN else None
    cf_ciclo_plan = fields_plan.get("ciclo", {}).get("id") if LIST_PLAN else None
    cf_cli_pgm = fields_pgm.get("cliente", {}).get("id") if LIST_PGM else None
    dedupe_reu = build_dedupe_index(tasks_reu, cf_cli_reu, cf_ciclo_reu)
    dedupe_plan = build_dedupe_index(tasks_plan, cf_cli_plan, cf_ciclo_plan)
    dedupe_pgm = build_dedupe_pgm_index(tasks_pgm, cf_cli_pgm)

    print(f"{len(specialists)} especialistas  |  "
          f"reuniões existentes: {len(existing_reu)} (dedupe chaves: {len(dedupe_reu)}) | "
          f"planejamentos: {len(existing_plan)} (dedupe: {len(dedupe_plan)}) | "
          f"pgm: {len(existing_pgm)} (dedupe: {len(dedupe_pgm)})\n")

    # LIMIT: processa só os N primeiros especialistas
    if LIMIT is not None:
        specialists = specialists[:LIMIT]

    for task in specialists:
        process_specialist(task, today, year, plano_opts_by_value,
                           fields_reu, fields_plan, fields_pgm,
                           existing_reu, existing_plan, existing_pgm,
                           dedupe_reu, dedupe_plan, dedupe_pgm, task_types)

    print("\n━━━ Concluído ━━━")


if __name__ == "__main__":
    main()
