FROM python:3.12-slim
WORKDIR /app
COPY rpl_flashscore_proxy.py rpl_arena_v6.html ./
ENV PORT=8080 CACHE_DIR=/app/.cache PYTHONUNBUFFERED=1
VOLUME /app/.cache
EXPOSE 8080
CMD ["python", "rpl_flashscore_proxy.py"]
