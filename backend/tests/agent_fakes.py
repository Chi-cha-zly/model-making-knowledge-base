"""Scripted tool-calling chat model for agent tests; never touches the network."""

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class ScriptedChatModel(BaseChatModel):
    """Returns queued AIMessages in order and records every prompt it saw."""

    script: list = []
    seen: list = []

    @property
    def _llm_type(self):
        return 'scripted'

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        if not self.script:
            raise AssertionError('Scripted model ran out of responses')
        message = self.script.pop(0)
        if callable(message):
            message = message(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name, args, id='c1'):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': id}])


def scripted(*messages):
    return ScriptedChatModel(script=list(messages), seen=[])
