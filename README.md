# STEP — Sync de Testes A/B de Conteúdo

Automação que conecta a lista **Planejamento de conteúdo N1**, as listas de produção (**Copy conteúdo**, **Design/Edição**, **Agendamentos**) e a lista **Testes A/B de Conteúdo**.

## O que ela faz

### Fluxo 1 — Quando você marca `executar teste`
1. Você adiciona a etiqueta `executar teste` na Tarefa 1 (original) e preenche o campo **"Tipo de teste A/B"**.
2. A cada 5 min, o script:
   - Cria a **Tarefa 2** (novo conteúdo a ser publicado) na lista de Planejamento, com etiqueta `teste a/b` e **todos os campos copiados** da Tarefa 1 (Cliente, Rede Social, Link do Post Original, Tipo, Editorias, Data da Postagem, Legenda).
   - Cria a **Tarefa 3** (registro) na lista Testes A/B, com os mesmos campos + o campo "Tipo de teste" preenchido.
   - Vincula as 3 tarefas (botão "Relacionadas" aparece nativo no ClickUp).
   - Marca a Tarefa 1 com etiqueta `teste processado` e remove `executar teste`, para não processar de novo.

### Fluxo 2 — Sincronização de status
- Tarefa 2 em **Copy conteúdo** ou **Design/Edição** → Tarefa 3 fica em **"em produção"**.
- Tarefa 2 em **Agendamentos** → Tarefa 3 vai para **"análise"** e a data de vencimento = Data da Postagem.

---

## Pré-requisitos (parte manual — 5 minutos)

### 1. Adicionar o campo "Tipo de teste" na lista Planejamento

O campo já existe na lista Testes A/B. Você precisa **tornar ele visível também** na lista Planejamento (é o mesmo campo, basta habilitar):

1. Vá em **Planejamento de conteúdo N1** no ClickUp.
2. Clique em qualquer tarefa → aba "Custom Fields".
3. Na barra de campos, clique em **"+ Add field"** → **"Add existing field"**.
4. Procure por **"Tipo de teste"** e adicione.

Alternativa: pelas configurações da lista, em "Custom Fields" → "Add existing".

### 2. Criar as etiquetas no workspace (se ainda não existem)

- `executar teste`
- `teste processado`
- `teste a/b` (você já usa)

### 3. Gerar um token da API do ClickUp

1. No ClickUp: ícone do seu perfil (canto inferior esquerdo) → **Apps**.
2. Em **API Token**, clique em **Generate**.
3. Copie o token (começa com `pk_...`).

---

## Deploy no GitHub Actions (5 minutos, grátis)

### 1. Criar o repositório
1. Vá em github.com → "New repository" → pode ser **privado**, nome `step-abtest`.
2. Não precisa adicionar nada, deixe vazio.

### 2. Subir estes arquivos
Na raiz do seu repo, faça upload de:
- `ab_test_sync.py`
- `requirements.txt`
- `.github/workflows/sync.yml`

(Dá pra fazer pela interface web do GitHub mesmo — "Add file" → "Upload files".)

### 3. Adicionar o token como secret
1. No repositório: **Settings** → **Secrets and variables** → **Actions**.
2. **New repository secret**.
3. Nome: `CLICKUP_API_TOKEN`
4. Valor: o `pk_...` do passo anterior.
5. **Add secret**.

### 4. Ativar
- A action já vem com cron de 5 em 5 min.
- Para testar agora, vá em **Actions** → **Sync Testes A/B** → **Run workflow**.

---

## Como testar pela primeira vez

1. Vá em uma tarefa qualquer na lista **Planejamento de conteúdo N1** (escolha uma de teste, não uma real).
2. Preencha o campo **"Tipo de teste"** (ex: Headline).
3. Adicione a etiqueta **`executar teste`**.
4. Vá em **Actions** → **Run workflow** (ou espere até 5 min).
5. Confira:
   - A Tarefa 1 agora tem etiqueta `teste processado`.
   - Existe uma nova Tarefa 2 na lista Planejamento com etiqueta `teste a/b` e todos os campos copiados.
   - Existe uma Tarefa 3 na lista Testes A/B, com campo "Tipo de teste" preenchido.
   - As 3 tarefas estão vinculadas entre si (você vê na aba "Relationships" de qualquer uma).

---

## Observações importantes

### Status da lista Testes A/B
O script assume que existem os status `EM PRODUÇÃO` e `ANÁLISE` (em maiúsculo, com acentos). Se na sua lista os nomes forem diferentes, edite no topo de `ab_test_sync.py`:

```python
STATUS_TESTEAB_EM_PRODUCAO = "EM PRODUÇÃO"
STATUS_TESTEAB_ANALISE = "ANÁLISE"
```

### Custo
GitHub Actions é grátis para repositórios públicos e dá 2000 minutos/mês grátis em privados. Uma execução roda em ~20 segundos. Rodando de 5 em 5 min = 8640 execuções/mês × 20s = ~48h. Isso excede o grátis.

**Recomendação:** deixe o repositório **público** (o código em si não tem nada sensível — o token fica no Secret). Aí é grátis para sempre.

Se preferir privado, mude o cron para 15 min (`*/15 * * * *`) — fica bem abaixo do limite grátis.

### Quando NÃO vai rodar
- Se você marcar `executar teste` mas esquecer de preencher "Tipo de teste" → o script pula e loga um aviso (visível na aba Actions do GitHub). A tarefa fica na fila até você preencher.

### Se der erro
Aba **Actions** do GitHub mostra o log completo de cada execução. Se algo quebrar, o log aponta qual tarefa e qual chamada falhou.
