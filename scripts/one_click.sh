#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/_ops_common.sh"

cd "$(ombre_repo_root)"

LOCAL_COMPOSE_FILE="compose.local.yml"

line() {
  printf '%s\n' '------------------------------------------------------------'
}

pause() {
  printf '\n按 Enter 返回菜单...'
  read -r _ || true
}

backup_file() {
  local path="$1"
  [[ -f "${path}" ]] || return 0
  local stamp
  stamp="$(date +%Y%m%d_%H%M%S)"
  cp "${path}" "${path}.bak.${stamp}"
  printf '已备份 %s -> %s.bak.%s\n' "${path}" "${path}" "${stamp}"
}

prompt_text() {
  local label="$1"
  local default="$2"
  local value
  if [[ -n "${default}" ]]; then
    read -r -p "${label} [${default}]: " value
    printf '%s\n' "${value:-${default}}"
  else
    read -r -p "${label}: " value
    printf '%s\n' "${value}"
  fi
}

prompt_yes_no() {
  local label="$1"
  local default="$2"
  local value
  local suffix="[y/N]"
  [[ "${default}" == "y" ]] && suffix="[Y/n]"
  while true; do
    read -r -p "${label} ${suffix}: " value
    value="${value:-${default}}"
    case "${value}" in
      y|Y|yes|YES) return 0 ;;
      n|N|no|NO) return 1 ;;
      *) printf '请输入 y 或 n。\n' ;;
    esac
  done
}

prompt_secret() {
  local label="$1"
  local required="${2:-false}"
  local value
  while true; do
    read -r -s -p "${label}: " value
    printf '\n'
    if [[ -n "${value}" || "${required}" != "true" ]]; then
      printf '%s\n' "${value}"
      return 0
    fi
    printf '这个值必填。\n'
  done
}

random_token() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 24
  elif command -v uuidgen >/dev/null 2>&1; then
    uuidgen | tr -d '-'
  else
    printf 'ombre-%s-%s\n' "$(date +%s)" "$RANDOM"
  fi
}

yaml_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '"%s"' "${value}"
}

env_line() {
  local key="$1"
  local value="$2"
  printf '%s=%s\n' "${key}" "${value}"
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "${value}"
}

slugify() {
  local value="$1"
  value="$(printf '%s' "${value}" | tr '[:upper:]' '[:lower:]')"
  value="$(printf '%s' "${value}" | sed -E 's/[^a-z0-9]+/-/g; s/^-+//; s/-+$//')"
  printf '%s' "${value:-provider}"
}

env_prefix_for_provider() {
  local value="$1"
  value="$(printf '%s' "${value}" | tr '[:lower:]' '[:upper:]')"
  value="$(printf '%s' "${value}" | sed -E 's/[^A-Z0-9]+/_/g; s/^_+//; s/_+$//')"
  printf 'OMBRE_GATEWAY_%s' "${value:-PROVIDER}"
}

declare -a GW_NAMES=()
declare -a GW_SLUGS=()
declare -a GW_BASE_URLS=()
declare -a GW_DEFAULT_MODELS=()
declare -a GW_MODELS=()
declare -a GW_KEY_ENVS=()
GATEWAY_ENV_LINES=""
GATEWAY_UPSTREAMS_YAML=""

append_gateway_env() {
  local key="$1"
  local value="$2"
  GATEWAY_ENV_LINES+="${key}=${value}"$'\n'
}

add_gateway_provider_interactive() {
  local index="$1"
  local default_name="$2"
  local default_base_url="$3"
  local default_model="$4"
  local dehy_key="$5"

  line
  printf 'Gateway 上游 #%s\n' "${index}"

  local name slug base_url models_raw default_model_value prefix key_envs key_value key_count
  name="$(prompt_text 'Provider 名称（用于同名模型自动别名）' "${default_name}")"
  slug="$(slugify "${name}")"
  base_url="$(prompt_text 'Provider base_url' "${default_base_url}")"
  models_raw="$(prompt_text '模型列表（多个用英文逗号分隔）' "${default_model}")"
  default_model_value="$(prompt_text '默认模型' "$(trim "${models_raw%%,*}")")"

  prefix="$(env_prefix_for_provider "${name}")"
  key_envs=""
  if prompt_yes_no '这个 Provider 要配置多 key 吗' 'n'; then
    key_count="$(prompt_text 'key 数量' '2')"
    if ! [[ "${key_count}" =~ ^[0-9]+$ ]] || (( key_count < 1 )); then
      key_count=2
    fi
    for ((i = 1; i <= key_count; i++)); do
      local env_name="${prefix}_API_KEY_${i}"
      key_value="$(prompt_secret "${name} 第 ${i} 个 key（${env_name}）" true)"
      append_gateway_env "${env_name}" "${key_value}"
      key_envs+="${env_name},"
    done
  else
    local env_name="${prefix}_API_KEY"
    if [[ "${index}" == "1" ]] && prompt_yes_no '这个 Provider 的 key 复用脱水 key 吗' 'y'; then
      key_value="${dehy_key}"
    else
      key_value="$(prompt_secret "${name} key（${env_name}）" true)"
    fi
    append_gateway_env "${env_name}" "${key_value}"
    key_envs="${env_name},"
  fi
  key_envs="${key_envs%,}"

  GW_NAMES+=("${name}")
  GW_SLUGS+=("${slug}")
  GW_BASE_URLS+=("${base_url}")
  GW_DEFAULT_MODELS+=("${default_model_value}")
  GW_MODELS+=("${models_raw}")
  GW_KEY_ENVS+=("${key_envs}")
}

build_gateway_upstreams_yaml() {
  local yaml=$'  upstreams:\n'
  local duplicate_count=0
  declare -A model_counts=()

  for models_raw in "${GW_MODELS[@]}"; do
    IFS=',' read -r -a models <<< "${models_raw}"
    for raw_model in "${models[@]}"; do
      local model
      model="$(trim "${raw_model}")"
      [[ -z "${model}" ]] && continue
      model_counts["${model}"]=$(( ${model_counts["${model}"]:-0} + 1 ))
    done
  done

  for ((idx = 0; idx < ${#GW_NAMES[@]}; idx++)); do
    yaml+="    - name: $(yaml_quote "${GW_NAMES[$idx]}")"$'\n'
    yaml+="      base_url: $(yaml_quote "${GW_BASE_URLS[$idx]}")"$'\n'
    IFS=',' read -r -a key_envs <<< "${GW_KEY_ENVS[$idx]}"
    if (( ${#key_envs[@]} > 1 )); then
      yaml+="      api_key_envs:"$'\n'
      for env_name in "${key_envs[@]}"; do
        env_name="$(trim "${env_name}")"
        [[ -z "${env_name}" ]] && continue
        yaml+="        - $(yaml_quote "${env_name}")"$'\n'
      done
    else
      yaml+="      api_key_env: $(yaml_quote "${key_envs[0]}")"$'\n'
    fi
    yaml+="      default_model: $(yaml_quote "${GW_DEFAULT_MODELS[$idx]}")"$'\n'
    yaml+="      prompt_cache: \"\""$'\n'
    yaml+="      models:"$'\n'
    IFS=',' read -r -a models <<< "${GW_MODELS[$idx]}"
    for raw_model in "${models[@]}"; do
      local model alias
      model="$(trim "${raw_model}")"
      [[ -z "${model}" ]] && continue
      if (( ${model_counts["${model}"]:-0} > 1 )); then
        duplicate_count=$((duplicate_count + 1))
        alias="${GW_SLUGS[$idx]}/${model}"
        yaml+="        - id: $(yaml_quote "${alias}")"$'\n'
        yaml+="          upstream_model: $(yaml_quote "${model}")"$'\n'
      else
        yaml+="        - $(yaml_quote "${model}")"$'\n'
      fi
    done
  done

  if (( duplicate_count > 0 )); then
    printf '检测到同名模型，已自动写成 provider/模型名 的 Gateway alias。\n' >&2
  fi
  printf '%s' "${yaml}"
}

configure_gateway_upstreams() {
  local dehy_base_url="$1"
  local dehy_model="$2"
  local dehy_key="$3"
  local choice count

  GW_NAMES=()
  GW_SLUGS=()
  GW_BASE_URLS=()
  GW_DEFAULT_MODELS=()
  GW_MODELS=()
  GW_KEY_ENVS=()
  GATEWAY_ENV_LINES=""

  line
  printf 'Gateway 模型和 key 配置\n'
  printf '1. 单上游：复用脱水模型站点\n'
  printf '2. 单上游：自定义站点\n'
  printf '3. 多上游：多个 provider，可分别配置多 key\n'
  read -r -p '输入（1-3）：' choice
  case "${choice}" in
    1)
      add_gateway_provider_interactive 1 "deepseek" "${dehy_base_url}" "${dehy_model}" "${dehy_key}"
      ;;
    2)
      add_gateway_provider_interactive 1 "provider-a" "${dehy_base_url}" "${dehy_model}" "${dehy_key}"
      ;;
    3)
      count="$(prompt_text 'Provider 数量' '2')"
      if ! [[ "${count}" =~ ^[0-9]+$ ]] || (( count < 1 )); then
        count=2
      fi
      for ((i = 1; i <= count; i++)); do
        add_gateway_provider_interactive "${i}" "provider-${i}" "${dehy_base_url}" "${dehy_model}" "${dehy_key}"
      done
      ;;
    *)
      printf '未识别输入，按单上游复用脱水模型站点处理。\n'
      add_gateway_provider_interactive 1 "deepseek" "${dehy_base_url}" "${dehy_model}" "${dehy_key}"
      ;;
  esac

  GATEWAY_UPSTREAMS_YAML="$(build_gateway_upstreams_yaml)"
}

write_env_file() {
  local dehy_key="$1"
  local embedding_key="$2"
  local gateway_token="$3"
  local dream_key="$4"
  local persona_key="$5"
  local reflection_key="$6"

  backup_file ".env"
  {
    printf '# Generated by scripts/one_click.sh\n'
    env_line "OMBRE_API_KEY" "${dehy_key}"
    env_line "OMBRE_EMBEDDING_API_KEY" "${embedding_key}"
    env_line "OMBRE_GATEWAY_TOKEN" "${gateway_token}"
    env_line "OMBRE_PERSONA_API_KEY" "${persona_key}"
    env_line "OMBRE_REFLECTION_API_KEY" "${reflection_key}"
    env_line "OMBRE_DREAM_API_KEY" "${dream_key}"
    if [[ -n "${GATEWAY_ENV_LINES}" ]]; then
      printf '%s' "${GATEWAY_ENV_LINES}"
    fi
  } > ".env"
  printf '已写入 .env（不会显示 key）\n'
}

write_config_file() {
  local ai_name="$1"
  local user_name="$2"
  local user_display_name="$3"
  local dehy_base_url="$4"
  local dehy_model="$5"
  local embedding_enabled="$6"
  local embedding_base_url="$7"
  local embedding_model="$8"
  local gateway_upstreams_yaml="$9"
  local dream_enabled="${10}"
  local dream_base_url="${11}"
  local dream_model="${12}"
  local dream_probability="${13}"
  local brain_port="${14}"
  local gateway_port="${15}"

  backup_file "config.yaml"
  cat > "config.yaml" <<EOF
transport: "streamable-http"
log_level: "INFO"

identity:
  ai_name: $(yaml_quote "${ai_name}")
  user_name: $(yaml_quote "${user_name}")
  user_display_name: $(yaml_quote "${user_display_name}")
  user_aliases:
    - "宝宝"
    - "老婆"
    - "亲爱的"
    - "她"

dehydration:
  model: $(yaml_quote "${dehy_model}")
  base_url: $(yaml_quote "${dehy_base_url}")
  thinking_mode: ""
  max_tokens: 1024
  temperature: 0.1

embedding:
  enabled: ${embedding_enabled}
  model: $(yaml_quote "${embedding_model}")
  base_url: $(yaml_quote "${embedding_base_url}")

gateway:
  host: "0.0.0.0"
  port: 8010
  default_session_id: "main"
  head_recent_hours: 72
  dynamic_top_k: 10
  inject_max_cards: 2
  skip_recent_rounds: 5
  cooldown_hours: 6
  cooldown_floor: 0.3
  inject_total_budget: 1200
  recent_context_budget: 300
  recalled_memory_budget: 400
  relationship_weather_budget: 220
  favorite_memory_budget: 180
  favorite_memory_max_cards: 1
  related_memory_budget: 220
  current_inner_state_interval_rounds: 15
  relationship_weather_interval_rounds: 0
  favorite_memory_interval_rounds: 0
  upstream_key_cooldown_seconds: 300
${gateway_upstreams_yaml}

persona:
  enabled: true
  profile_id: "main"
  mode: "llm"
  base_url: $(yaml_quote "${dehy_base_url}")
  model: $(yaml_quote "${dehy_model}")
  thinking_mode: ""
  temperature: 0.1
  max_tokens: 500

reflection:
  enabled: true
  auto_enabled: true
  enrich_on_write: true
  enrich_backfill_enabled: true
  enrich_backfill_limit: 5
  base_url: $(yaml_quote "${dehy_base_url}")
  model: $(yaml_quote "${dehy_model}")
  thinking_mode: ""
  timezone: "Asia/Shanghai"
  daily_hour: 4
  check_interval_minutes: 60

dream:
  enabled: ${dream_enabled}
  auto_enabled: ${dream_enabled}
  surface_enabled: true
  base_url: $(yaml_quote "${dream_base_url}")
  model: $(yaml_quote "${dream_model}")
  thinking_mode: "disabled"
  temperature: 0.85
  max_tokens: 900
  timezone: "Asia/Shanghai"
  daily_hour: 3
  run_window_hours: 3
  daily_probability: ${dream_probability}
  check_interval_minutes: 60
  min_material_count: 5
  material_window_hours: 48
  material_limit: 5
  old_echo_enabled: true
  old_echo_min_age_hours: 72
  min_surface_age_hours: 3
  surface_threshold: 0.62
  attempt_threshold: 0.45
  spontaneous_surface_prob: 0.02

# Host ports used by compose.local.yml:
#   Ombre-Brain: http://127.0.0.1:${brain_port}
#   Gateway:     http://127.0.0.1:${gateway_port}
EOF
  printf '已写入 config.yaml\n'
}

write_compose_file() {
  local brain_port="$1"
  local gateway_port="$2"

  backup_file "${LOCAL_COMPOSE_FILE}"
  cat > "${LOCAL_COMPOSE_FILE}" <<EOF
services:
  ombre-brain:
    build: .
    container_name: ombre-brain
    restart: unless-stopped
    command: ["python", "server.py"]
    env_file:
      - .env
    environment:
      OMBRE_TRANSPORT: streamable-http
      OMBRE_BUCKETS_DIR: /data
      OMBRE_STATE_DIR: /state
      OMBRE_GATEWAY_ADMIN_URL: http://ombre-gateway:8010/api/config
    ports:
      - "${brain_port}:8000"
    volumes:
      - ./buckets:/data
      - ./state:/state
      - ./config.yaml:/app/config.yaml:ro

  ombre-gateway:
    build: .
    container_name: ombre-gateway
    restart: unless-stopped
    command: ["python", "gateway.py"]
    env_file:
      - .env
    environment:
      OMBRE_TRANSPORT: streamable-http
      OMBRE_BUCKETS_DIR: /data
      OMBRE_STATE_DIR: /state
    ports:
      - "${gateway_port}:8010"
    volumes:
      - ./buckets:/data
      - ./state:/state
      - ./config.yaml:/app/config.yaml:ro
EOF
  printf '已写入 %s\n' "${LOCAL_COMPOSE_FILE}"
}

ensure_tools() {
  if ! command -v docker >/dev/null 2>&1; then
    printf '未找到 docker，请先安装 Docker。\n'
    return 1
  fi
  if ! docker compose version >/dev/null 2>&1 && ! command -v docker-compose >/dev/null 2>&1; then
    printf '未找到 docker compose / docker-compose。\n'
    return 1
  fi
  return 0
}

first_deploy() {
  line
  printf '首次部署会生成 .env、config.yaml、%s，并启动容器。\n' "${LOCAL_COMPOSE_FILE}"
  printf '已有同名文件会先备份。\n'
  line
  ensure_tools || return 1

  local ai_name user_name user_display_name
  ai_name="$(prompt_text 'AI 名字' 'Haven')"
  user_name="$(prompt_text '用户英文/内部名' 'Rain')"
  user_display_name="$(prompt_text '用户显示名' '小雨')"

  local dehy_base_url dehy_model dehy_key
  dehy_base_url="$(prompt_text '脱水/导入抽取 base_url' 'https://api.deepseek.com/v1')"
  dehy_model="$(prompt_text '脱水/导入抽取模型' 'deepseek-chat')"
  dehy_key="$(prompt_secret '脱水模型 key（OMBRE_API_KEY，必填）' true)"

  local embedding_enabled embedding_base_url embedding_model embedding_key
  if prompt_yes_no '启用 embedding 语义检索吗' 'y'; then
    embedding_enabled="true"
    embedding_base_url="$(prompt_text 'embedding base_url' 'https://api.siliconflow.cn/v1')"
    embedding_model="$(prompt_text 'embedding 模型' 'Qwen/Qwen3-Embedding-0.6B')"
    embedding_key="$(prompt_secret 'embedding key（OMBRE_EMBEDDING_API_KEY，建议必填）' true)"
  else
    embedding_enabled="false"
    embedding_base_url=""
    embedding_model=""
    embedding_key=""
  fi

  local gateway_token
  configure_gateway_upstreams "${dehy_base_url}" "${dehy_model}" "${dehy_key}"
  gateway_token="$(prompt_secret 'Gateway 访问 token（回车自动生成）' false)"
  if [[ -z "${gateway_token}" ]]; then
    gateway_token="$(random_token)"
    printf '已自动生成 OMBRE_GATEWAY_TOKEN。\n'
  fi

  local dream_enabled dream_base_url dream_model dream_key dream_probability
  if prompt_yes_no '启用夜梦吗' 'y'; then
    dream_enabled="true"
    dream_base_url="$(prompt_text '夜梦 base_url' 'https://api.deepseek.com')"
    dream_model="$(prompt_text '夜梦模型' 'deepseek-v4-flash')"
    dream_probability="$(prompt_text '做梦概率（0-1）' '0.4')"
    if prompt_yes_no '夜梦 key 复用脱水 key 吗' 'y'; then
      dream_key="${dehy_key}"
    else
      dream_key="$(prompt_secret '夜梦 key（OMBRE_DREAM_API_KEY）' true)"
    fi
  else
    dream_enabled="false"
    dream_base_url="https://api.deepseek.com"
    dream_model="deepseek-v4-flash"
    dream_probability="0"
    dream_key=""
  fi

  local persona_key reflection_key
  persona_key="$(prompt_secret 'Persona key（可回车，默认复用 OMBRE_API_KEY）' false)"
  reflection_key="$(prompt_secret 'Reflection key（可回车，默认复用 OMBRE_API_KEY/Persona）' false)"

  local brain_port gateway_port
  brain_port="$(prompt_text 'Ombre-Brain 对外端口' '18001')"
  gateway_port="$(prompt_text 'Gateway 对外端口' '18002')"

  write_env_file "${dehy_key}" "${embedding_key}" "${gateway_token}" "${dream_key}" "${persona_key}" "${reflection_key}"
  write_config_file \
    "${ai_name}" "${user_name}" "${user_display_name}" \
    "${dehy_base_url}" "${dehy_model}" \
    "${embedding_enabled}" "${embedding_base_url}" "${embedding_model}" \
    "${GATEWAY_UPSTREAMS_YAML}" \
    "${dream_enabled}" "${dream_base_url}" "${dream_model}" "${dream_probability}" \
    "${brain_port}" "${gateway_port}"
  write_compose_file "${brain_port}" "${gateway_port}"

  mkdir -p buckets state

  export COMPOSE_FILE="${LOCAL_COMPOSE_FILE}"
  export HEALTH_URL="http://127.0.0.1:${brain_port}/health"
  printf '\n开始构建并启动容器...\n'
  "${SCRIPT_DIR}/update_deploy.sh"
}

choose_compose_file() {
  local default="${COMPOSE_FILE:-}"
  if [[ -z "${default}" ]]; then
    if [[ -f "${LOCAL_COMPOSE_FILE}" ]]; then
      default="${LOCAL_COMPOSE_FILE}"
    else
      default="$(ombre_compose_file)"
    fi
  fi
  COMPOSE_FILE="$(prompt_text 'Compose 文件' "${default}")"
  export COMPOSE_FILE
}

update_version() {
  choose_compose_file
  "${SCRIPT_DIR}/update_deploy.sh"
}

run_doctor() {
  choose_compose_file
  "${SCRIPT_DIR}/doctor.sh"
}

maintenance_menu() {
  local choice
  while true; do
    line
    printf '==== 池又雨二改版 Ombre 维护工具 ====\n'
    printf '1. 补缺失 embedding\n'
    printf '2. 重建全部 embedding\n'
    printf '3. 检查并清理孤儿 embedding\n'
    printf '0. 返回上一级\n'
    if ! read -r -p '输入（0-3）：' choice; then
      printf '\n'
      return 0
    fi
    case "${choice}" in
      1) choose_compose_file; "${SCRIPT_DIR}/embedding_backfill.sh"; pause ;;
      2) choose_compose_file; "${SCRIPT_DIR}/embedding_rebuild.sh"; pause ;;
      3) choose_compose_file; "${SCRIPT_DIR}/embedding_cleanup_orphans.sh"; pause ;;
      0) return 0 ;;
      *) printf '请输入 0-3。\n' ;;
    esac
  done
}

main_menu() {
  local choice
  while true; do
    line
    printf '==== 池又雨二改版 Ombre 一键部署脚本 ====\n'
    printf '1. 首次部署\n'
    printf '2. 更新版本\n'
    printf '3. 错误排查\n'
    printf '4. 常用维护\n'
    printf '0. 退出\n'
    if ! read -r -p '输入（0-4）：' choice; then
      printf '\n'
      exit 0
    fi
    case "${choice}" in
      1) first_deploy; pause ;;
      2) update_version; pause ;;
      3) run_doctor; pause ;;
      4) maintenance_menu ;;
      0) exit 0 ;;
      *) printf '请输入 0-4。\n' ;;
    esac
  done
}

if [[ "${OMBRE_ONE_CLICK_SOURCE_ONLY:-}" != "1" ]]; then
  main_menu
fi
