FROM python:3.12-slim

# Сертификаты Минцифры нужны для исходящих HTTPS-запросов к platform-api2.max.ru
# (MAX Bot API подписан этим CA, системный trust store его не знает по умолчанию)
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends ca-certificates curl \
    && curl -fsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
        -o /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
    && curl -fsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
        -o /usr/local/share/ca-certificates/russian_trusted_sub_ca.crt \
    && update-ca-certificates \
    && apt-get purge -y -qq curl && apt-get autoremove -y -qq && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY paragraph_bot ./paragraph_bot

CMD ["uv", "run", "python", "-m", "paragraph_bot", "run"]
