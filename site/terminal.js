const output = document.querySelector('#terminal-output');
const form = document.querySelector('#terminal-form');
const input = document.querySelector('#terminal-command');
const modeButtons = [...document.querySelectorAll('.mode-button')];
const modeLabel = document.querySelector('#mode-label');
const telemetryMode = document.querySelector('#telemetry-mode');
const clock = document.querySelector('#session-clock');

let mode = 'llm';
let startedAt = Date.now();
let busy = false;

function addLine(className, text, prefix = '') {
  const line = document.createElement('div');
  line.className = `log-line ${className}`;
  if (prefix) {
    const marker = document.createElement('span');
    marker.className = 'log-time';
    marker.textContent = prefix;
    line.append(marker, document.createTextNode(' '));
  }
  line.append(document.createTextNode(text));
  output.append(line);
  output.scrollTop = output.scrollHeight;
  return line;
}

function setMode(nextMode) {
  mode = nextMode;
  const label = mode.toUpperCase();
  modeLabel.textContent = label;
  telemetryMode.textContent = label;
  modeButtons.forEach((button) => {
    const selected = button.dataset.mode === mode;
    button.classList.toggle('is-active', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  addLine('log-system', `Режим переключён: ${label}.`, '[mode]');
}

function responseFor(text) {
  const normalized = text.toLowerCase();
  if (normalized.includes('привет')) return 'На связи. Напиши задачу или используй /help, чтобы посмотреть команды демо.';
  if (normalized.includes('план') || mode === 'agi') {
    return 'Цель: ' + text + '\nШаги: 1) уточнить ограничения; 2) разбить на этапы; 3) проверить риски; 4) зафиксировать критерии результата. Это демонстрационный ответ.';
  }
  if (normalized.includes('бот') || normalized.includes('telegram')) {
    return 'Для Telegram-бота сначала зафиксируй сценарии, затем подключи Bot API, очередь запросов и безопасное хранение ключей на сервере.';
  }
  return `Запрос принят в режиме ${mode.toUpperCase()}. В реальном приложении ответ придёт от модели; здесь показан локальный пример интерфейса.`;
}

function handleCommand(value) {
  const [command, ...args] = value.trim().split(/\s+/);
  if (command === '/help') {
    addLine('log-answer', 'Доступно: /help, /status, /mode llm, /mode agi, /clear. Или введи любой текстовый запрос.');
    return true;
  }
  if (command === '/status') {
    addLine('log-answer', `Сессия READY · режим ${mode.toUpperCase()} · очередь 0/1 · подключение к API отключено.`);
    return true;
  }
  if (command === '/mode') {
    const requested = args[0]?.toLowerCase();
    if (requested === 'llm' || requested === 'agi') setMode(requested);
    else addLine('log-error', 'Укажи режим: /mode llm или /mode agi.');
    return true;
  }
  if (command === '/clear') {
    output.replaceChildren();
    addLine('log-system', 'Консоль очищена. Сессия продолжена.', '[готово]');
    return true;
  }
  return false;
}

function submitRequest(value) {
  const text = value.trim();
  if (!text) return;
  if (text.startsWith('/')) {
    handleCommand(text);
    return;
  }
  if (busy) {
    addLine('log-error', 'Запрос уже обрабатывается. Подожди завершения текущей задачи.');
    return;
  }
  busy = true;
  addLine('log-user', text, 'you');
  const thinking = addLine('log-system', 'Обработка запроса…', '[queue 1/1]');
  const frames = ['Обработка запроса…', 'Анализирую задачу…', 'Формирую ответ…'];
  let frameIndex = 0;
  const animation = window.setInterval(() => {
    frameIndex = (frameIndex + 1) % frames.length;
    thinking.textContent = `[thinking] ${frames[frameIndex]}`;
  }, 550);
  window.setTimeout(() => {
    window.clearInterval(animation);
    thinking.remove();
    addLine('log-answer', responseFor(text), '[vega]');
    busy = false;
  }, 1500);
}

modeButtons.forEach((button) => button.addEventListener('click', () => setMode(button.dataset.mode)));
form.addEventListener('submit', (event) => {
  event.preventDefault();
  submitRequest(input.value);
  input.value = '';
});
document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
    event.preventDefault();
    output.replaceChildren();
    addLine('log-system', 'Консоль очищена. Сессия продолжена.', '[готово]');
    input.focus();
  }
});
window.setInterval(() => {
  const elapsed = Math.floor((Date.now() - startedAt) / 1000);
  const minutes = String(Math.floor(elapsed / 60)).padStart(2, '0');
  const seconds = String(elapsed % 60).padStart(2, '0');
  clock.textContent = `SESSION ${minutes}:${seconds}`;
}, 1000);