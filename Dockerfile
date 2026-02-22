FROM php:8.2-cli

RUN apt-get update && apt-get install -y libcurl4-openssl-dev && \
    docker-php-ext-install curl pcntl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY autochat.php .

CMD ["php", "autochat.php"]
