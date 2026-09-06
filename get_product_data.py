import requests
import json

# --- ВСТАВЬ СВОЙ НОВЫЙ SECRET KEY ---
# (Тот самый, который в curl выдал 403. Значит он рабочий!)
API_SECRET = "9a42365ef382f4d0e0bf007df42dddbe8a7182b4" 

# Ссылка на товар (Amazon)
PRODUCT_URL = "https://www.amazon.com/dp/B0C2P3KPTM/" 

# --- ПРАВИЛЬНЫЙ ENDPOINT (Бесплатный/Affiliated) ---
# Обрати внимание: это GET запрос, а не POST
url = "https://api.sovrn.com/commerce/price-comparison/affiliated"

# Заголовки (точно такие же, как в твоем curl)
headers = {
    "Authorization": f"secret {API_SECRET}",
    "Content-Type": "application/json",
    "Accept": "application/json"
}

# Параметры (для GET запроса)
params = {
    "plainlink": PRODUCT_URL,
    "limit": 5
}

print(f"Отправляем запрос на: {url}")

try:
    # Важно: используем requests.get
    response = requests.get(url, headers=headers, params=params)
    
    print(f"Статус ответа: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print("\n--- УСПЕХ! ДАННЫЕ ПОЛУЧЕНЫ ---")
        # Выводим красиво JSON
        # print(json.dumps(data, indent=2)) 
        
        # Пытаемся найти картинку в ответе
        found = False
        if isinstance(data, list):
            for item in data:
                # Sovrn может вернуть разные поля для картинки, ищем любое
                img = item.get('imageUrl') or item.get('image') or item.get('thumbnailUrl')
                title = item.get('title')
                price = item.get('price')
                
                if img:
                    print(f"\nТовар: {title}")
                    print(f"Цена: {price}")
                    print(f"КАРТИНКА: {img}")
                    found = True
                    break # Берем первую попавшуюся
        
        if not found:
            print("\nОтвет от API пришел, но картинки внутри нет. Возможно, товара нет в базе Sovrn.")
            print("Сырой ответ:", response.text)
            
    elif response.status_code == 403:
        print("\nОШИБКА 403: Доступ запрещен.")
        print("Это странно для Affiliated API. Возможно, нужно активировать API в настройках аккаунта.")
        
    else:
        print(f"\nОшибка API: {response.status_code}")
        print("Текст ошибки:", response.text)

except Exception as e:
    print(f"Критическая ошибка скрипта: {e}")