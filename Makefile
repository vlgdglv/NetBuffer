.PHONY: run stop logs restart clean shell

run:
	docker compose up -d --build

stop:
	docker compose down

logs:
	docker compose logs -f

restart:
	docker compose restart

shell:
	docker compose exec netbuffer /bin/bash

clean:
	docker system prune -f

ps:
	docker compose ps