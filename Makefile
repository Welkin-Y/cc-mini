.PHONY: build run bash

build:
	docker-compose build

run:
	docker-compose run --rm cc-mini

bash:
	docker-compose run --rm cc-mini bash
