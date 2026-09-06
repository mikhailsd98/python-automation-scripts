from google import genai

API_KEY = "AIzaSyA-pBPECS91lPpG_TfS82i5jlRj2LPbcLU"
client = genai.Client(api_key=API_KEY)

print("🔍 Список всех доступных моделей (ищем 'image', 'imagen' или 'vision'):\n")

try:
    # Получаем список всех моделей
    model_list = client.models.list()

    # Выводим их, ища ключевые слова
    for model in model_list:
        model_name = model.name.lower()
        if "image" in model_name or "imagen" in model_name or "vision" in model_name:
            print(f"**💡 Возможно, это то, что нужно:** {model.name}")
        else:
            print(f"- {model.name}")

except Exception as e:
    print(f"Критическая ошибка при получении списка моделей: {e}")