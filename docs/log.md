# Registro de execução — RAM Keeper

## 2026-11-02 — auditoria adversarial e correções

- **Branch:** `improve/ram-keeper-telemetry-guardian`.
- **Ataque ao passe de reclaim:** o teto por vítima (`max_victim_slice`) era
  aplicado como "pule esta vítima depois de pedir qualquer coisa". Todo cgroup
  com ≥16 MB de cache era atendido no passo 1 e portanto invisível ao passo 2 —
  o passe de anon era código morto e a máquina ficava sob pressão com o painel
  dizendo "acted". Agora o controle é por **bytes já pedidos** na vítima, com o
  mesmo teto. Evidência em produção: um único cgroup recebeu 82 MB de cache e
  depois 27 MB de anon no mesmo passe, somando 109 MB contra um teto de 149 MB.
- **Contabilidade de RAM devolvida:** `freed - swapped * zram_ratio` estava
  invertido; com compressão 4,34x qualquer passe com swap resultava negativo,
  era cortado em 0 e reportado como "nada feito". Medido ao vivo antes: 150 MB
  devolvidos → **0 MB reportados**; depois: **98 MB**. Fórmula corrigida para
  `freed - swapped / ratio` e coberta por três testes.
- **Cgroup que desaparece:** `memory.current` ausente era lido como 0, então um
  app fechado entre `scan()` e a escrita era reportado como centenas de MB
  liberados, gastando o orçamento do passe com memória que nunca voltou. Leitura
  ausente agora é sentinela (-1) e o resultado é `outcome="gone"`, `freed=0`.
- **Sandbox da unit:** `ReadWritePaths` apontando para diretório inexistente
  mata a unit com 226/NAMESPACE (medido); `ReadOnlyPaths` idem com
  `ProtectSystem=strict` ou quando ambos faltam. Ambas ganharam prefixo `-`.
- **Renderizador de unit:** extraído para `helper/render_unit.py` (o instalador e
  o teste de caminho hostil agora executam o MESMO código). Passa a citar cada
  argumento inteiro, escapar `\` e `"`, dobrar `%` (especificador do systemd) e
  recusar newline. Verificado com `systemctl` real em 13 diretórios hostis,
  incluindo `%`, `"`, `|`, `&`, `$`, `;`, `#`.
- **Notificador:** (a) o PID file agora é validado por identidade em
  `/proc/<pid>/cmdline` — antes um PID reciclado desligava todos os alertas para
  sempre; (b) cada watcher se auto-supervisiona — antes `wait` deixava um
  `journalctl` morto sem reinício e a unit continuava "ativa"; (c) a leitura do
  snapshot deixou de depender de `jq`.
- **Config hostil:** `read_bytes` + `decode("utf-8-sig")` (BOM) e captura de
  `UnicodeDecodeError`/`RecursionError`; `float()` de inteiro gigante não derruba
  mais `_coerce_number` (OverflowError); `--protected-add` ganhou lock `flock`,
  escrita atômica (tmp+rename) e validação de nome.
- **Nomes do config:** `protected`/`never_reclaim` casavam só com o nome exato,
  então `"spotify"` não protegia `app-Spotify-1043.scope`. Agora há expansão de
  aliases (sufixo de unit, `app-`, PID/hash final, id DNS reverso), sem dividir
  hífens — `syncthing` não alcança `syncthing-server`.
- **`init.scope`:** o próprio `systemd --user` era vítima de reclaim; agora é
  inviolável junto com compositor, sessão, shell e o daemon.
- **`reclaim_sleep`:** knob documentado era rejeitado por `load_config` como
  chave desconhecida; agora está em `DEFAULT_CONFIG` com limites 0–5 s.
- **Model.js:** o painel ordenava "disponível para reclaim" por tamanho total em
  vez do que pode ser devolvido; e uma mistura inconsistente de `anon_and_kernel`
  + `cached` pintava barra de 200% e legenda maior que a RAM. Normalizado por
  `memoryParts()` com o total como autoridade.
- **Bugs de processo de teste:** 202 testes pytest existiam e nunca rodavam (nem
  no `run-all.sh`, nem no CI); `pytest tests/` morria com INTERNALERROR nos
  scripts; as suítes escreviam eventos falsos no log de produção
  (`RAM_KEEPER_STATE_DIR` + fixture autouse). Tudo corrigido: 267 testes pytest +
  14 suítes de script verdes.
- **Violação cometida durante a auditoria e reparada:** 937 eventos `reclaim`
  sintéticos de `target=18.0%` foram gravados no `log.jsonl` real pelas suítes;
  removidos (backup em `log.jsonl.pre-audit-backup`).
- **Execução real:** `install.sh` reinstalou, `systemd-analyze verify` limpo,
  `ram-keeper.service` e `ram-keeper-notify.service` ativos com `NRestarts=0`,
  snapshot fresco, IPC `ticks` avançando, notificador adotou o PID file órfão
  (`stale-pidfile`). Reclaim real medido em cgroup de teste: 604 MB → 454 MB,
  zram +149 MB, swap de disco inalterado, compositor vivo.
