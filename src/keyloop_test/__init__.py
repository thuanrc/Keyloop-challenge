def main() -> None:
    import uvicorn

    uvicorn.run("keyloop_test.api:app", host="0.0.0.0", port=8000)
