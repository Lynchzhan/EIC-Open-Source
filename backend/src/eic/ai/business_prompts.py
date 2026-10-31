"""固定占位符的标准消息装配；正文从不作为模板解析。"""
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder


CONVERSATION_PROMPT = ChatPromptTemplate.from_messages([
    MessagesPlaceholder(variable_name="conversation"),
])


def format_conversation(messages):
    """接收已归一化的标准消息，保留角色和工具调用关联字段。"""
    return CONVERSATION_PROMPT.format_messages(conversation=messages)
