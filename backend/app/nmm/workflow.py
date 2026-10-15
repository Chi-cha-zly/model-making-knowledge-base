"""JSON-only LangGraph orchestration, with side-effect-free interrupt node.

interrupt replays its node on resume: keep all persisted work in other nodes.
https://docs.langchain.com/oss/python/langgraph/interrupts
"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt


class WorkflowState(TypedDict, total=False):
    project_id: str
    revision: int
    params: dict
    attempt: int
    analysis: dict
    candidate_ids: list[str]
    selected_candidate_id: str
    palette: list[dict]
    metadata: list[dict]
    plan: dict
    step_ids: list[str]
    warnings: list[str]
    decision: dict


def build_workflow(handler, checkpointer):
    graph = StateGraph(WorkflowState)
    for name in ('analyze', 'generate', 'palette', 'plan', 'render', 'refine', 'review'):

        async def node(state: WorkflowState, stage=name) -> dict[str, Any]:
            return await handler(stage, state)

        graph.add_node(name, node)

    def confirm(state):
        decision = interrupt(
            {
                'project_id': state['project_id'],
                'revision': state['revision'],
                'candidate_ids': state['candidate_ids'],
            }
        )
        output = {'decision': decision}
        if decision['action'] == 'select':
            output['selected_candidate_id'] = decision['candidate_id']
        return output

    graph.add_node('confirm', confirm)
    graph.add_edge(START, 'analyze')
    graph.add_edge('analyze', 'generate')
    graph.add_edge('generate', 'confirm')
    graph.add_conditional_edges(
        'confirm',
        lambda state: 'generate' if state['decision']['action'] == 'regenerate' else 'palette',
    )
    for first, second in (
        ('palette', 'plan'),
        ('plan', 'render'),
        ('render', 'refine'),
        ('refine', 'review'),
    ):
        graph.add_edge(first, second)
    graph.add_edge('review', END)
    return graph.compile(checkpointer=checkpointer)
