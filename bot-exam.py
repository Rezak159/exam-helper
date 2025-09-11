import telebot
from telebot.types import Message, BotCommand
from telebot.types import ReplyKeyboardMarkup, KeyboardButton
from telebot import types
from config import TOKEN_TG, TOKEN_AI
from groq import Groq
import os
import json
import logging
import random
import re
import time

# Настройка логирования
logging.basicConfig(level=logging.ERROR)
for logger_name in ("httpx", "telebot"):
    logging.getLogger(logger_name).setLevel(logging.ERROR)

logger = logging.getLogger(__name__)

# Инициализация
bot = telebot.TeleBot(TOKEN_TG)
client = Groq(api_key=TOKEN_AI)
DEFAULT_MODEL = 'openai/gpt-oss-120b'

# Константы
USER_STATS_FILE = "user_stats.json"
USER_MESSAGES_FILE = "user_messages.json"
EXAM_STATE_FILE = "exam_states.json"
USER_QUESTION_STATS_FILE = "user_question_stats.json"
MAX_CONTEXT_LENGTH = 3000

# Конфигурация тем экзамена
EXAM_TOPICS = {
    "python": {
        "questions_file": "answers_python.json",
        "display_name": "Питончик 🐍"
    },
    "Текст": {
        "questions_file": "answers_graph.json", 
        "display_name": "текст 🔢"
    },
    "clash royale": {
        "questions_file": "answers_royale.json", 
        "display_name": "Клещ рояль 🐞"
    }
}


# Глобальные переменные
user_messages = {}
user_stats = {}
answers_data = {}
user_exam_state = {}
user_question_stats = {}

# ======================== УТИЛИТЫ ========================

def load_data(filename):
    """Загрузка данных из JSON файла"""
    if os.path.exists(filename):
        with open(filename, "r", encoding='utf-8') as file:
            return json.load(file)
    return {}

def save_data(filename, data):
    """Сохранение данных в JSON файл"""
    with open(filename, "w", encoding='utf-8') as file:
        json.dump(data, file, ensure_ascii=False, indent=4)

def save_exam_state():
    """Сохранение состояний экзамена"""
    save_data(EXAM_STATE_FILE, user_exam_state)

def load_topic_data(topic_key):
    """Загрузка данных для темы (возвращает словарь)"""
    if topic_key in EXAM_TOPICS:
        questions_file = EXAM_TOPICS[topic_key]["questions_file"]
        return load_data(questions_file)
    return {}

def get_user_questions(user_id):
    """Получить вопросы пользователя из состояния"""
    user_id_str = str(user_id)
    if user_id_str in user_exam_state:
        return user_exam_state[user_id_str].get("questions", {})
    return {}


def split_message(text, max_length=4096):
    """Разбивка длинного сообщения на части"""
    return [text[i:i + max_length] for i in range(0, len(text), max_length)]

def trim_context(context):
    """Обрезка контекста до лимита"""
    current_length = sum(len(msg["content"]) for msg in context)
    while current_length > MAX_CONTEXT_LENGTH and context:
        context.pop(0)
        current_length = sum(len(msg["content"]) for msg in context)
    return context

def remove_think_blocks(text):
    """Удаление блоков размышлений модели"""
    return re.sub(r'<think>[\s\S]*?</think>', '', text, flags=re.IGNORECASE)

def send_message_safe(chat_id, text, markup=None):
    """Безопасная отправка сообщения"""
    try:
        bot.send_message(chat_id, text, parse_mode='Markdown', reply_markup=markup)
    except Exception:
        bot.send_message(chat_id, text, reply_markup=markup)

def parse_ai_score(ai_response):
    """
    Извлекает процент оценки из ответа ИИ
    Ищет паттерн: "Оценка: XX%"
    Возвращает число 0-100 или None при ошибке
    """
    # Основной паттерн: "Оценка: XX%"
    match = re.search(r"Оценка:\s*(\d{1,3})%", ai_response, re.IGNORECASE)
    if match:
        score = int(match.group(1))
        # Проверяем валидность (0-100%)
        if 0 <= score <= 100:
            return score
    
    # Запасной паттерн: просто "XX%" в начале строки
    match = re.search(r"^(\d{1,3})%", ai_response.strip())
    if match:
        score = int(match.group(1))
        if 0 <= score <= 100:
            return score
            
    return None

# ======================== КЛАВИАТУРЫ ========================

def get_main_keyboard():
    """Основная клавиатура"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row(KeyboardButton("📚 Начать экзамен"))
    keyboard.row(KeyboardButton("📊 Статистика"), KeyboardButton("🗑 Очистить историю"))
    return keyboard

def get_exam_keyboard():
    """Клавиатура для экзамена"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.row(KeyboardButton("📖 Показать теорию"))
    keyboard.row(KeyboardButton("⏭️ Следующий вопрос"), KeyboardButton("❌ Завершить экзамен"))
    return keyboard

def get_hidden_keyboard():
    """Скрытая клавиатура"""
    return types.ReplyKeyboardRemove()

# ======================== ИНИЦИАЛИЗАЦИЯ ========================

def initialize_user(user_id, user_data):
    """Инициализация пользователя"""
    user_id_str = str(user_id)
    if user_id_str not in user_stats:
        user_stats[user_id_str] = {
            "username": user_data.get('username', 'Unknown'),
            "text_requests": 0,
            "voice_requests": 0,
            "model": DEFAULT_MODEL,
            "exam_answered": 0
        }
        save_data(USER_STATS_FILE, user_stats)
    
    if user_id_str not in user_messages:
        user_messages[user_id_str] = []

def load_all_data():
    """Загрузка всех данных при старте"""
    global user_messages, user_stats, answers_data, user_exam_state, user_question_stats
    user_messages = load_data(USER_MESSAGES_FILE)
    user_stats = load_data(USER_STATS_FILE)
    user_exam_state = load_data(EXAM_STATE_FILE)
    user_question_stats = load_data(USER_QUESTION_STATS_FILE)

# ======================== ЭКЗАМЕН ========================

import hashlib

def get_question_hash(question_text):
    """Создает уникальный хеш для вопроса"""
    return hashlib.md5(question_text.encode('utf-8')).hexdigest()[:12]

def add_score_to_question(user_id, topic_key, question_text, score, max_history=5):
    """Добавляет оценку к вопросу пользователя"""
    user_id_str = str(user_id)
    question_hash = get_question_hash(question_text)
    
    # Инициализируем структуру если нужно
    if user_id_str not in user_question_stats:
        user_question_stats[user_id_str] = {}
    if topic_key not in user_question_stats[user_id_str]:
        user_question_stats[user_id_str][topic_key] = {}
    
    # Получаем текущий список оценок
    scores_list = user_question_stats[user_id_str][topic_key].get(question_hash, [])
    
    # Добавляем новую оценку
    scores_list.append(score)
    
    # Ограничиваем размер истории
    if len(scores_list) > max_history:
        scores_list.pop(0)
    
    # Сохраняем
    user_question_stats[user_id_str][topic_key][question_hash] = scores_list
    save_data(USER_QUESTION_STATS_FILE, user_question_stats)

def get_average_score(user_id, topic_key, question_text):
    """Получает средний балл пользователя по вопросу"""
    user_id_str = str(user_id)
    question_hash = get_question_hash(question_text)
    
    if (user_id_str in user_question_stats and 
        topic_key in user_question_stats[user_id_str] and
        question_hash in user_question_stats[user_id_str][topic_key]):
        
        scores = user_question_stats[user_id_str][topic_key][question_hash]
        return sum(scores) / len(scores) if scores else 0
    
    return 0  # Новый вопрос

def select_adaptive_question(user_id, topic_key, available_questions):
    """Выбирает вопрос на основе статистики пользователя"""
    user_id_str = str(user_id)
    
    # Собираем веса для всех вопросов
    weights = []
    questions = list(available_questions.keys())
    
    for question in questions:
        avg_score = get_average_score(user_id, topic_key, question)
        
        # Формула: чем ниже средний балл, тем выше вес
        # Коэффициент 2.0 усиливает разницу
        weight = (100 - avg_score) ** 2.0
        weight = max(weight, 1)  # Минимальный вес = 1
        
        weights.append(weight)
    
    # Выбираем с учетом весов
    import random
    selected_question = random.choices(questions, weights=weights, k=1)[0]
    
    return selected_question

def get_topics_keyboard():
    """Клавиатура для выбора темы экзамена"""
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True)
    
    # Добавляем темы по 2 в ряд
    topics = list(EXAM_TOPICS.keys())
    for i in range(0, len(topics), 2):
        if i + 1 < len(topics):
            keyboard.row(
                KeyboardButton(EXAM_TOPICS[topics[i]]["display_name"]),
                KeyboardButton(EXAM_TOPICS[topics[i+1]]["display_name"])
            )
        else:
            keyboard.row(KeyboardButton(EXAM_TOPICS[topics[i]]["display_name"]))
    
    keyboard.row(KeyboardButton("🔙 Назад в меню"))
    return keyboard

def start_exam(user_id, chat_id, topic_key):
    """Начало экзамена по выбранной теме"""
    user_id_str = str(user_id)
    
    # Загружаем вопросы для темы
    questions_data = load_topic_data(topic_key)
    if not questions_data:
        bot.send_message(chat_id, f"❌ Ошибка: База вопросов для темы '{EXAM_TOPICS[topic_key]['display_name']}' пуста или не найдена.", reply_markup=get_main_keyboard())
        return
    
    question = random.choice(list(questions_data.keys()))
    
    # Сохраняем ВСЕ вопросы в состоянии пользователя
    user_exam_state[user_id_str] = {
        "question": question,
        "waiting_answer": True,
        "topic": topic_key,
        "topic_display": EXAM_TOPICS[topic_key]["display_name"],
        "start_time": time.time(),
        "questions": questions_data  # 👈 Сохраняем все вопросы здесь!
    }
    save_exam_state()  # Записываем в файл
    
    bot.send_message(
        chat_id,
        f"🎯 Экзамен начат!\n\n"
        f"Тема: {EXAM_TOPICS[topic_key]['display_name']}\n"
        f"Вопрос:\n{question}\n\n"
        f"💬 Введите ваш ответ:",
        reply_markup=get_hidden_keyboard()
    )


def process_exam_answer(user_id, chat_id, user_answer):
    """Обработка ответа на экзамен"""
    user_id_str = str(user_id)
    question = user_exam_state[user_id_str]["question"]
    topic_key = user_exam_state[user_id_str]["topic"]
    
    # Получаем правильный ответ
    user_questions = get_user_questions(user_id)
    correct_answer = user_questions.get(question, "")
    
    # Меняем состояние
    user_exam_state[user_id_str]["waiting_answer"] = False
    user_exam_state[user_id_str]["waiting_action"] = True
    save_exam_state()  # Сохраняем изменения
    
    # Увеличиваем счетчик
    user_stats[user_id_str]["exam_answered"] = user_stats[user_id_str].get("exam_answered", 0) + 1
    save_data(USER_STATS_FILE, user_stats)

    
    # Оценка ответа
    prompt = (
        f"Вопрос: {question}\n"
        f"Эталонный ответ (образец): {correct_answer}\n"
        f"Ответ пользователя: {user_answer}\n\n"
        f"Оцени, насколько ответ пользователя совпадает с эталонным (от 0% до 100%). Не будь слишком строгим."
        f"Кратко укажи, взяв из эталона, чего не хватает в ответе пользователя, а что хорошо.\n"
        f"Формат ответа:\nОценка: <проценты>%\nРекомендация: <текст>"
    )
    
    try:
        chat_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=user_stats[user_id_str].get("model", DEFAULT_MODEL),
        )
        response = chat_completion.choices[0].message.content
        response = remove_think_blocks(response)

        # 👈 НОВОЕ: Парсим оценку и сохраняем статистику
        score = parse_ai_score(response)
        if score is not None:
            add_score_to_question(user_id, topic_key, question, score)
            logger.info(f"Saved score {score} for user {user_id}, question: {question[:50]}...")
        else:
            logger.warning(f"Failed to parse score from AI response: {response[:100]}...")
        
        # Отправляем оценку и показываем клавиатуру экзамена
        send_message_safe(chat_id, f"📝 Результат:\n\n{response}", get_exam_keyboard())
        
    except Exception as e:
        bot.send_message(chat_id, f"❌ Ошибка при оценке ответа: {e}", reply_markup=get_exam_keyboard())

def show_theory(user_id, chat_id):
    """Показ теории по вопросу"""
    user_id_str = str(user_id)
    question = user_exam_state[user_id_str].get("question", "")
    
    # Берем правильный ответ из сохраненных вопросов пользователя
    user_questions = get_user_questions(user_id)
    correct_answer = user_questions.get(question, "")
    
    theory_prompt = (
        f"Ты — преподаватель информатики. На основе следующего экзаменационного вопроса и эталонного ответа "
        f"составь компактный, но полный конспект по теме для подготовки к экзамену. "
        f"Излагай структурировано с подзаголовками, списками и короткими примерами кода, где уместно.\n\n"
        f"Вопрос: {question}\n"
        f"Эталонный ответ: {correct_answer}\n\n"
        f"Требования к структуре:\n"
        f"1) Краткое введение в тему (1–2 предложения)\n"
        f"2) Ключевые понятия и определения\n"
        f"3) Основные приёмы/синтаксис/формулы (по теме)\n"
        f"4) Короткие примеры (минимум 2)\n"
        f"5) Частые ошибки и как их избегать\n"
        f"6) Мини-чеклист перед экзаменом\n\n"
        f"Выводи строго на русском языке. Заголовок: 'Теория по теме'."
    )
    
    try:
        # Сообщаем пользователю, что идёт формирование теории
        thinking_message = bot.send_message(chat_id, '🤔 Думаю над теорией...')

        theory_completion = client.chat.completions.create(
            messages=[{"role": "user", "content": theory_prompt}],
            model=user_stats[user_id_str].get("model", DEFAULT_MODEL),
        )
        theory = theory_completion.choices[0].message.content
        theory = remove_think_blocks(theory)
        
        # Отправляем теорию частями, первую часть подставляем в сообщение "думаю"
        message_parts = split_message(theory)
        if message_parts:
            try:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=thinking_message.message_id,
                    text=message_parts[0],
                    parse_mode='Markdown'
                )
            except:
                bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=thinking_message.message_id,
                    text=message_parts[0]
                )
            for part in message_parts[1:]:
                send_message_safe(chat_id, part)
            
    except Exception as e:
        # Пытаемся заменить сообщение "думаю" на ошибку, если оно было отправлено
        try:
            bot.edit_message_text(
                chat_id=chat_id,
                message_id=thinking_message.message_id,
                text=f"❌ Ошибка при формировании теории: {e}"
            )
        except:
            bot.send_message(chat_id, f"❌ Ошибка при формировании теории: {e}")

def next_question(user_id, chat_id):
    """Следующий вопрос"""
    user_id_str = str(user_id)
    topic_key = user_exam_state[user_id_str]["topic"]
    
    # Берем вопросы из состояния пользователя
    user_questions = get_user_questions(user_id)
    if not user_questions:
        bot.send_message(chat_id, "❌ Ошибка: Нет доступных вопросов.")
        return
    
    question = select_adaptive_question(user_id, topic_key, user_questions)
    topic_display = user_exam_state[user_id_str]["topic_display"]
    
    # Обновляем состояние
    user_exam_state[user_id_str].update({
        "question": question,
        "waiting_answer": True,
        "waiting_action": False
    })
    save_exam_state()  # Сохраняем изменения
    
    bot.send_message(
        chat_id,
        f"📋 Следующий вопрос ({topic_display}):\n\n{question}\n\n💬 Введите ваш ответ:",
        reply_markup=get_hidden_keyboard()
    )


def end_exam(user_id, chat_id):
    """Завершение экзамена"""
    user_id_str = str(user_id)
    
    # Просто удаляем состояние - все данные автоматически исчезают
    user_exam_state.pop(user_id_str, None)
    save_exam_state()
    
    bot.send_message(
        chat_id,
        "✅ Экзамен завершён!\n\nВы можете начать новый экзамен или использовать другие функции бота.",
        reply_markup=get_main_keyboard()
    )


# ======================== ОБРАБОТЧИКИ КОМАНД ========================

@bot.message_handler(commands=['start'])
def cmd_start(message: Message):
    user_id = message.from_user.id
    initialize_user(user_id, message.from_user.__dict__)
    
    welcome_text = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        f"Я ИИ-бот для подготовки к экзаменам по информатике.\n\n"
        f"Что я умею:\n"
        f"• 🎯 Проводить экзамены с оценкой ответов\n"
        f"• 📖 Показывать теорию по темам\n"
        f"• 🎤 Обрабатывать голосовые сообщения\n"
        f"• 💬 Отвечать на вопросы\n\n"
        f"Выберите действие на клавиатуре ниже!"
    )
    
    send_message_safe(message.chat.id, welcome_text, get_main_keyboard())

@bot.message_handler(commands=['help'])
def cmd_help(message: Message):
    help_text = (
        "📖 Справка по командам:\n\n"
        "🎯 `/exam` — начать экзамен\n"
        "📊 `/settings` — статистика и настройки\n"
        "🗑 `/clear` — очистить историю диалога\n"
        "❌ `/cancel_exam` — отменить текущий экзамен\n\n"
        "Или используйте кнопки на клавиатуре!"
    )
    bot.send_message(message.chat.id, help_text)

@bot.message_handler(commands=['clear'])
def cmd_clear(message: Message):
    user_id = message.from_user.id
    initialize_user(user_id, message.from_user.__dict__)
    user_messages[str(user_id)] = []
    save_data(USER_MESSAGES_FILE, user_messages)
    bot.send_message(message.chat.id, '🗑 История диалога очищена!', reply_markup=get_main_keyboard())

@bot.message_handler(commands=['settings'])
def cmd_settings(message: Message):
    user_id = message.from_user.id
    initialize_user(user_id, message.from_user.__dict__)
    stats = user_stats[str(user_id)]
    
    stats_text = (
        f"📊 Ваша статистика:\n\n"
        f"👤 Пользователь: {stats['username']}\n"
        f"💬 Текстовые запросы: {stats['text_requests']}\n"
        f"🎤 Голосовые запросы: {stats['voice_requests']}\n"
        f"🧠 Модель ИИ: {stats.get('model', DEFAULT_MODEL)}\n"
        f"📝 Экзаменационных ответов: {stats.get('exam_answered', 0)}"
    )
    
    send_message_safe(message.chat.id, stats_text, get_main_keyboard())

@bot.message_handler(commands=['exam'])
def cmd_exam(message: Message):
    user_id = message.from_user.id
    initialize_user(user_id, message.from_user.__dict__)
    
    user_id_str = str(user_id)
    
    # Проверяем активный экзамен
    if user_id_str in user_exam_state and user_exam_state[user_id_str].get("waiting_answer"):
        current_question = user_exam_state[user_id_str]["question"]
        current_topic = user_exam_state[user_id_str].get("topic_display", "Неизвестно")
        bot.send_message(
            message.chat.id,
            f"❗ У вас есть незавершенный экзамен!\n\n"
            f"Тема: {current_topic}\n"
            f"Вопрос: {current_question}\n\n"
            f"💬 Введите ваш ответ или используйте /cancel_exam для отмены:",
            reply_markup=get_hidden_keyboard()
        )
        return
    
    # Показываем выбор темы
    user_exam_state[user_id_str] = {"waiting_topic": True}
    save_exam_state()
    
    topics_text = "🎯 Выберите тему для экзамена:\n\n"
    for topic_key, topic_data in EXAM_TOPICS.items():
        questions_count = len(load_topic_data(topic_key))
        topics_text += f"• {topic_data['display_name']} ({questions_count} вопросов)\n"
    
    send_message_safe(message.chat.id, topics_text, get_topics_keyboard())


@bot.message_handler(commands=['cancel_exam'])
def cmd_cancel_exam(message: Message):
    user_id = message.from_user.id
    user_id_str = str(user_id)
    
    if user_id_str in user_exam_state:
        user_exam_state.pop(user_id_str)
        save_exam_state()
        bot.send_message(message.chat.id, "❌ Экзамен отменен!", reply_markup=get_main_keyboard())
    else:
        bot.send_message(message.chat.id, "❌ У вас нет активного экзамена.", reply_markup=get_main_keyboard())

# ======================== ОБРАБОТЧИК ТЕКСТА ========================

@bot.message_handler(content_types=['text'])
def handle_text(message: Message):
    user_id = message.from_user.id
    user_id_str = str(user_id)
    text = message.text.strip()
    
    initialize_user(user_id, message.from_user.__dict__)

    # Обработка выбора темы
    if user_id_str in user_exam_state and user_exam_state[user_id_str].get("waiting_topic"):
        # Ищем тему по display_name
        selected_topic = None
        for topic_key, topic_data in EXAM_TOPICS.items():
            if text == topic_data["display_name"]:
                selected_topic = topic_key
                break
        
        if selected_topic:
            user_exam_state[user_id_str].pop("waiting_topic", None)
            start_exam(user_id, message.chat.id, selected_topic)  # 👈 Теперь с topic_key!
            return
        elif text == "🔙 Назад в меню":
            user_exam_state.pop(user_id_str, None)
            save_exam_state()
            send_message_safe(message.chat.id, "↩️ Возврат в главное меню", get_main_keyboard())
            return
        else:
            bot.send_message(message.chat.id, "❌ Неверный выбор. Пожалуйста, выберите тему из предложенных:")
            return
    
    # Обработка кнопок клавиатуры
    if text == "📚 Начать экзамен":
        cmd_exam(message)
        return
    
    elif text == "📊 Статистика":
        cmd_settings(message)
        return
    
    elif text == "🗑 Очистить историю":
        cmd_clear(message)
        return
    
    # Обработка экзамена
    if user_id_str in user_exam_state:
        exam_state = user_exam_state[user_id_str]
        
        # Если ждем ответ на вопрос
        if exam_state.get("waiting_answer"):
            process_exam_answer(user_id, message.chat.id, text)
            return
        
        # Если ждем действие после ответа
        elif exam_state.get("waiting_action"):
            if text == "📖 Показать теорию":
                show_theory(user_id, message.chat.id)
                return
            
            elif text == "⏭️ Следующий вопрос":
                next_question(user_id, message.chat.id)
                return
            
            elif text == "❌ Завершить экзамен":
                end_exam(user_id, message.chat.id)
                return
    
    # Обычное общение с ИИ
    user_stats[user_id_str]["text_requests"] += 1
    save_data(USER_STATS_FILE, user_stats)
    
    sent_message = bot.send_message(message.chat.id, '🤔 Думаю...')
    
    # Сохраняем сообщение пользователя
    new_message = {"role": "user", "content": text}
    user_messages[user_id_str].append(new_message)
    
    # Обрезаем контекст
    context = trim_context(user_messages[user_id_str])
    
    try:
        # Запрос к ИИ
        chat_completion = client.chat.completions.create(
            messages=context,
            model=user_stats[user_id_str].get("model", DEFAULT_MODEL),
        )
        
        response = chat_completion.choices[0].message.content
        response = remove_think_blocks(response)
        
        # Сохраняем ответ бота
        bot_response = {"role": "assistant", "content": response}
        user_messages[user_id_str].append(bot_response)
        user_messages[user_id_str] = trim_context(user_messages[user_id_str])
        save_data(USER_MESSAGES_FILE, user_messages)
        
        # Отправляем ответ
        message_parts = split_message(response)
        
        # Редактируем первое сообщение
        try:
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=sent_message.message_id,
                text=message_parts[0],
                parse_mode='Markdown'
            )
        except:
            bot.edit_message_text(
                chat_id=message.chat.id,
                message_id=sent_message.message_id,
                text=message_parts[0]
            )
        
        # Отправляем остальные части
        for part in message_parts[1:]:
            send_message_safe(message.chat.id, part)
            
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка: {str(e)}\n\nИспользуйте /clear для сброса контекста.")

# ======================== ОБРАБОТЧИК ГОЛОСА ========================

@bot.message_handler(content_types=['voice'])
def handle_voice(message: Message):
    user_id = message.from_user.id
    user_id_str = str(user_id)
    
    initialize_user(user_id, message.from_user.__dict__)
    user_stats[user_id_str]["voice_requests"] += 1
    save_data(USER_STATS_FILE, user_stats)
    
    try:
        # Получаем и сохраняем аудио файл
        file_info = bot.get_file(message.voice.file_id)
        file_path = file_info.file_path
        audio_file = bot.download_file(file_path)
        audio_filename = f"{message.chat.id}_audio.ogg"
        
        with open(audio_filename, 'wb') as f:
            f.write(audio_file)
        
        # Транскрипция
        with open(audio_filename, 'rb') as audio:
            transcription = client.audio.transcriptions.create(
                file=(audio_filename, audio.read()),
                model="whisper-large-v3-turbo",
                response_format="json",
                language="ru",
                temperature=0.0
            )
        
        os.remove(audio_filename)  # Удаляем временный файл
        transcribed_text = transcription.text.strip()
        
        # Если в экзамене и ждем ответ
        if user_id_str in user_exam_state and user_exam_state[user_id_str].get("waiting_answer"):
            process_exam_answer(user_id, message.chat.id, transcribed_text)
            return
        
        # Обычная обработка
        bot.send_message(message.chat.id, f"🎤 Расшифровка: {transcribed_text}")
        
        # Сохраняем как обычное текстовое сообщение для контекста
        new_message = {"role": "user", "content": f"[Голосовое сообщение]: {transcribed_text}"}
        user_messages[user_id_str].append(new_message)
        save_data(USER_MESSAGES_FILE, user_messages)
        
    except Exception as e:
        bot.send_message(message.chat.id, f"❌ Ошибка при обработке голосового сообщения: {e}")

# ======================== ЗАПУСК ========================

def set_commands():
    """Установка команд бота"""
    commands = [
        BotCommand(command="start", description="Начать работу с ботом"),
        BotCommand(command="help", description="Справка по командам"),
        BotCommand(command="exam", description="Начать экзамен"),
        BotCommand(command="settings", description="Статистика и настройки"),
        BotCommand(command="clear", description="Очистить историю диалога"),
        BotCommand(command="cancel_exam", description="Отменить экзамен"),
    ]
    bot.set_my_commands(commands)

if __name__ == '__main__':
    print("🚀 Загрузка данных...")
    load_all_data()
    print("📋 Установка команд...")
    set_commands()
    print("✅ Бот запущен и готов к работе!")
    bot.polling(none_stop=True)
