import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


def build_menu(items, columns=2):
    buttons = [
        InlineKeyboardButton(text, callback_data=text)
        for text in items
    ]

    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(rows)
