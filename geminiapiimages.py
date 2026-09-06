from google import genai
from google.genai import types
from PIL import Image
import base64
from io import BytesIO
import os

API_KEY = os.getenv("AIzaSyA-pBPECS91lPpG_TfS82i5jlRj2LPbcLU")  # или вставьте строкой
client = genai.Client(api_key=API_KEY)

prompt = "Create a stylized 1024x1024 image of a small orange robot holding a bouquet of wildflowers, cinematic lighting"

# Генерация (только Image)
response = client.models.generate_content(
    model="gemini-2.5-flash-image",
    contents=[prompt],
    config=types.GenerateContentConfig(response_modalities=["Image"],
                                       image_config=types.ImageConfig(aspect_ratio="1:1", image_size="1K"))
)

# Пример доступа к ответу (структура зависит от версии SDK; может потребоваться смотреть response.generated_images)
# Ниже — общий вариант: ищем первое сгенерированное изображение и декодируем
candidates = response.candidates if hasattr(response, "candidates") else []
if not candidates:
    print("No image returned — проверьте ответ:", response)
else:
    # Вставьте правильный путь в структуре ответа, если SDK возвращает иначе
    image_b64 = candidates[0].content.parts[0].image.image_bytes  # примерная структура — проверьте по версии SDK
    # Если это base64-строка:
    image_bytes = base64.b64decode(image_b64)
    img = Image.open(BytesIO(image_bytes))
    img.save("out.png")
    print("Saved out.png")
