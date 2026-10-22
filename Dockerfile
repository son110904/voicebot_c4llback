FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Mặc định chạy seed rồi start Flask. Override CMD khi muốn chạy riêng.
EXPOSE 5000
CMD ["sh", "-c", "python seed_demo.py && python app.py"]
