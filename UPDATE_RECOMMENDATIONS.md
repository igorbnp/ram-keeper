# Update de Configuração do RAM Keeper Plugin

## Status Atual dos Serviços

| Serviço | Habilitado | Ativo | Memória | Proteção | Ação |
|---------|------------|-------|---------|----------|------|
| **ollama.service** | ✅ Sim | ✅ Sim | ~687 MB | ✅ Protegido | MANTER |
| **hermes-gateway.service** | ✅ Sim | ✅ Sim | ~304 MB | ✅ Protegido | MANTER (ou desativar se não usa) |
| **sia-brainstem.service** | ✅ Sim | ✅ Sim | ~217 MB | ✅ Protegido | MANTER (ou desativar se não usa) |
| **zenfree.service** | ✅ Sim | ✅ Sim | ~46 MB | ✅ Protegido | MANTER |
| **zenfree-opencode.service** | ❌ Não | ✅ Sim* | - | ✅ Protegido | OK |
| **syncthing.service** | - | - | - | - | Verificar |

_*Ativo por causa do `Wants=zenfree-opencode.service` no zenfree.service_

## Verificação de Proteção Aplicada

O ram-keeper agora está protegendo automaticamente estes processos:
- `hermes-gateway`
- `ollama`
- `sia-brainstem`
- `syncthing`
- `zenfree-opencode`
- `wayland-wm@hyprland.desktop` (compositor - protegido por sistema)

## Ações Recomendadas

### Para manter apenas serviços essenciais:

```bash
# Desativar Hermes Gateway (rede social/AI providers)
systemctl --user disable hermes-gateway.service
systemctl --user stop hermes-gateway.service

# Desativar SIA Brainstem (rede de armazenamento descentralizado)
systemctl --user disable sia-brainstem.service
systemctl --user stop sia-brainstem.service
```

### Para manter todos os serviços (configuração atual):

A configuração foi atualizada em `~/.config/ram-keeper/config.json` para proteger:
- `ollama.service` - servidor local de IA
- `hermes-gateway.service` - gateway de mensagens
- `sia-brainstem.service` - daemon da rede SIA
- `zenfree.service` - proxy OpenAI
- `zenfree-opencode.service` - servidor opencode
- `syncthing.service` - sincronização de arquivos (adicionar se usado)

### Para adicionar mais proteções:

```bash
# Adicionar um serviço à lista de proteção
python3 ~/.config/omarchy/plugins/ram-keeper/helper/ram_keeper.py --protected-add nome-do-servico.service
```

## Configuração Atual (`~/.config/ram-keeper/config.json`)

```json
{
  "target_free_percent": 20.0,
  "critical_free_percent": 10.0,
  "psi_some_percent": 6.0,
  "min_target_bytes": 150000000,
  "cache_first": true,
  "protected": [
    "wayland-wm@hyprland.desktop",
    "quickshell",
    "ram-keeper",
    "ollama.service",
    "syncthing.service",
    "hermes-gateway.service",
    "sia-brainstem.service",
    "zenfree-opencode.service",
    "zenfree.service"
  ]
}
```

## Resumo

✅ **Protegido (não será reclamado)**:
- Compositor (wayland-wm@hyprland.desktop)
- Shell (quickshell)
- Ram-keeper (o próprio daemon)
- Ollama (servidor local de IA)
- Hermes Gateway
- SIA Brainstem
- ZenFree/Opencode

✅ **Manter ativo se usado**:
- Ollama - se usa IA localmente
- ZenFree - se usa proxy OpenAI
- Syncthing - se sincroniza arquivos

❌ **Desativar se não usado**:
- Hermes Gateway - se não usa integração com redes sociais/AI providers
- SIA Brainstem - se não usa a rede Sia para armazenamento

## Verificação

Para verificar o estado atual:
```bash
python3 ~/.config/omarchy/plugins/ram-keeper/helper/ram_keeper.py --once | python3 -c 'import json,sys; d=json.load(sys.stdin); print(f"Estado: {d[\"state\"]}, Livre: {d[\"free_percent\"]}%")'
```