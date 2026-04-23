# Вместо bot.polling() использовать:
app.route('/webhook', methods=['POST'])
def webhook():
    bot.process_new_updates([telebot.types.Update
        .de_json(request.stream.read().decode("utf-8"))])
    return "OK"