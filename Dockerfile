FROM php:8.2-cli

# Extensions curl (incluse par défaut dans php:cli)
RUN apt-get update && apt-get install -y libcurl4-openssl-dev && \
    docker-php-ext-install curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY autochat.php .
COPY webhook.php .

CMD ["php", "autochat.php"]
