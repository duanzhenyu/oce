"""租户标识。"""

# 内置租户：env API_KEY 映射到它，存量数据迁移时全部归它；不可经 admin API 修改或删除。
DEFAULT_TENANT_ID = "default"
