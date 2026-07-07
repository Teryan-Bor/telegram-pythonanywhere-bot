from telegram import InlineKeyboardButton, InlineKeyboardMarkup

def build_menu(items, columns=2):
    buttons = [
        InlineKeyboardButton(text, callback_data=text)
        for text in items
    ]