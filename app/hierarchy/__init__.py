"""层次化电路模块库与组合验证。

- models.py  模块/组合网表解析、端口绑定校验、无环/递归检查
- flatten.py 层次网表确定性展开（并查集合并端口别名，保留实例路径）
- store.py   SQLite 持久化（模块版本不可变，组合网表，层次分析记录）
- routes.py  /api/hierarchy 路由
"""
