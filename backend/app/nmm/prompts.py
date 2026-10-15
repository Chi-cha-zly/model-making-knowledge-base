"""Structured context, deliberately separate from executable graph decisions."""

import json


def analysis_prompt(params):
    return json.dumps(
        {
            'task': 'analysis',
            'instruction': 'Analyze the editable miniature region and lighting. Return summary and warnings only.',
            'params': params,
        },
        ensure_ascii=False,
    )


def generation_prompt(params, analysis):
    return json.dumps(
        {
            'task': 'generate',
            'instruction': 'Repaint only the mask as hand-painted non-metallic metal. Preserve geometry and background; reference images only guide light and color.',
            'params': params,
            'analysis': analysis,
        },
        ensure_ascii=False,
    )


def plan_prompt(params, palette, steps):
    return json.dumps(
        {
            'task': 'plan',
            'instruction': 'Write one short practical instruction per supplied step in exact order. Use only supplied palette colors and actual area values; do not invent geometry or paint products.',
            'params': params,
            'palette': palette,
            'steps': steps,
        },
        ensure_ascii=False,
    )


def review_prompt(params, plan):
    return json.dumps(
        {
            'task': 'review',
            'instruction': 'Inspect the rendered teaching sequence and supplied instructions for regression, inconsistent_light, text_mismatch, unclear_area or other issues. Return issues(step,type,detail,severity) and summary; never modify images.',
            'params': params,
            'plan': plan,
        },
        ensure_ascii=False,
    )
