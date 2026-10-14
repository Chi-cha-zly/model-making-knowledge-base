"""Hand-written illustrative regression corpus, never inserted into the library.

Fictional product specifications are evaluation text, not compatibility guidance.
Judgments were authored alongside the questions: this is not a held-out benchmark.
"""
from copy import deepcopy

_ROWS = [
    ('technique', 1, '水口修整与砂纸打磨', '素组 水口 打磨 白痕', '先用剪钳保留少量流道，再二次剪切。用砂纸从粗到细逐级打磨水口，避免一次切入零件造成白痕。', '', []),
    ('technique', 2, '水贴定位和气泡处理', '贴花 水贴 气泡', '剪下水贴并浸水，滑到零件上定位。用棉签从中心向外滚压，排出水分和气泡，静置干燥。', '', []),
    ('technique', 3, '喷笔堵塞的清洁步骤', '喷涂 喷笔 清洁 堵塞', '停止喷涂，倒出剩余漆液，用匹配的清洁液清洗漆杯。拆下喷针擦拭，检查喷嘴堵塞，按说明重新装配。', '', []),
    ('technique', 4, '遮盖胶带防止渗色', '遮盖 分色 胶带 渗色', '在底色完全干燥后贴遮盖胶带，压实胶带边缘，薄喷多次。移除胶带前检查涂层状态。', '', []),
    ('technique', 5, '模型接缝填补', '无缝 补土 接缝', '对齐零件接缝并固定，填入少量补土，等完全固化后用砂纸磨平，检查凹陷后再处理。', '', []),
    ('technique', 6, '旧化干扫凸起边缘', '旧化 干扫 边缘', '笔尖蘸少量颜料，在纸巾上擦去大部分颜色，再轻扫凸起边缘，逐层增加磨损效果。', '', []),
    ('technique', 7, '刻线与沟槽清理', '刻线 沟槽 导向带', '用导向带确定直线，刻线针轻划多遍，清除沟槽里的碎屑，避免单次重压导致跑线。', '', []),
    ('technique', 8, '树脂零件脱模剂清洗', '树脂 脱模剂 清洗', '在独立容器中用温水和中性洗涤剂清洗树脂零件，以软刷清除脱模剂，再充分冲洗和晾干。', '', []),
    ('product', 1, '示例工坊 消光白涂料', '涂料 白色 消光', '示例编号 XF-2；别名消光白、哑光白。仅用于检索演示，未提供材质兼容性参数。', 'XF-2', ['消光白','哑光白']),
    ('product', 2, '示例工坊 消光黑涂料', '涂料 黑色 消光', '示例编号 XF-1；别名消光黑。仅用于检索演示。', 'XF-1', ['消光黑']),
    ('product', 3, '示例工坊 流缝胶', '胶水 粘接 流缝', '示例编号 MC-101；别名绿盖胶。流动性较高；是否适用具体材质需另查制造商资料。', 'MC-101', ['绿盖胶']),
    ('product', 4, '示例工坊 Primer 灰色底漆', '底漆 灰色 primer', '示例编号 PR-1000；别名灰底漆。作为检索词汇示例，不提供实际稀释配比。', 'PR-1000', ['灰底漆']),
    ('product', 5, '示例工坊 Sandpaper 打磨片', '工具 砂纸 sandpaper', '示例编号 SP-800；别名八百目砂纸。柔性背基用于局部修整。', 'SP-800', ['八百目砂纸']),
    ('product', 6, '示例工坊 Masking Tape 遮盖胶带', '工具 遮盖 masking tape', '示例编号 MT-06；别名六毫米胶带。用于分色时遮挡边界。', 'MT-06', ['六毫米胶带']),
]

# Explicit authored labels: no generation from retrieval rankings.
_CASES = [
    ('long-watergate','中文长问','我刚开始做模型，剪下来的零件上还有一块突出的水口，应该按什么顺序处理才能尽量减少白痕？',[('technique:1',3),('product:5',1)]),
    ('long-decals','中文长问','水贴已经放到零件上了，但是下面有气泡和水，棉签应该怎么操作？',[('technique:2',3)]),
    ('long-airbrush','中文长问','喷笔用完后发现喷不出来了，怀疑喷嘴堵塞，清洁漆杯和喷针要怎么做？',[('technique:3',3)]),
    ('long-masking','中文长问','分色喷涂时颜料总是从胶带边缘渗进去，贴遮盖胶带后应当怎样压边和喷涂？',[('technique:4',3),('product:6',1)]),
    ('long-seam','中文长问','两个零件拼好后接缝有凹陷，我想用补土填平，固化以后下一步做什么？',[('technique:5',3)]),
    ('sku-white','编号','XF-2',[('product:1',3)]),
    ('sku-black','编号','xf-1',[('product:2',3)]),
    ('sku-glue','编号','请找一下 MC-101 的资料',[('product:3',3)]),
    ('sku-primer','编号','PR-1000',[('product:4',3)]),
    ('alias-white','别名','哑光白',[('product:1',3)]),
    ('alias-glue','别名','绿盖胶',[('product:3',3)]),
    ('alias-paper','别名','八百目砂纸',[('product:5',3)]),
    ('alias-tape','别名','六毫米胶带',[('product:6',3)]),
    ('typo-primer','英文容错','primre',[('product:4',3)]),
    ('typo-paper','英文容错','sandpapre',[('product:5',3)]),
    ('typo-tape','英文容错','maskng tape',[('product:6',3)]),
    ('tag-weather','标签','旧化 干扫',[('technique:6',3)]),
    ('tag-scribe','标签','刻线 导向带',[('technique:7',3)]),
    ('tag-resin','标签','树脂 脱模剂',[('technique:8',3)]),
    ('step-brush','步骤','先在纸巾上擦去大部分颜色，然后轻扫凸起边缘',[('technique:6',3)]),
    ('step-groove','步骤','轻划多遍后，清除沟槽里的碎屑',[('technique:7',3)]),
    ('step-rinse','步骤','用温水和中性洗涤剂清洗，再冲洗晾干',[('technique:8',3)]),
    ('negative-food','无答案','番茄炒蛋应该放多少盐',[]),
    ('negative-electric','无答案','无人机电调固件如何升级',[]),
    ('negative-sku','无答案','ZZ-9999',[]),
    ('negative-generic','无答案','请帮我推荐一下，谢谢',[]),
    ('negative-empty','无答案','',[]),
    ('negative-space','无答案','量子纠缠卫星通信协议',[]),
]


def demo_fixture():
    documents = []
    for kind, id, title, tags, body, sku, aliases in _ROWS:
        source = {'id':1, 'title':'人工编写示例语料（非真实产品说明）', 'url':None}
        documents.append({'key':f'{kind}:{id}', 'type':kind, 'id':id, 'title':title,
                          'excerpt':body, 'text':f'{tags}\n{body}', 'sku':sku, 'aliases':aliases,
                          'fields':{'tags':tags, 'body':body}, 'source_ids':[1], 'sources':[source]})
    cases = [{'id':id, 'category':cat, 'query':query, 'judgments':[{'key':key,'grade':grade} for key,grade in labels]} for id,cat,query,labels in _CASES]
    dataset = {'id':'demo-v1', 'name':'模型制作检索示例集', 'description':'人工编写的演示回归集，含虚构产品；不是独立留出集或真实业务效果证明。',
               'corpus':'demo', 'builtin':True, 'case_count':len(cases), 'cases':cases}
    return deepcopy(dataset), deepcopy(documents)
