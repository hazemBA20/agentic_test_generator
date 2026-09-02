from langchain_nvidia_ai_endpoints import ChatNVIDIA


def init_chat_model():
    client = ChatNVIDIA(
    model="mistralai/mistral-7b-instruct-v0.2",
    api_key="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", 
    temperature=1,
    top_p=1,
    max_tokens=16384,
    seed=42,
    
    )

    for chunk in client.stream([{"role":"user","content":"who are you?"}]):
    
        print(chunk.content, end="")
    return client



if __name__ == "__main__":
    init_chat_model()  