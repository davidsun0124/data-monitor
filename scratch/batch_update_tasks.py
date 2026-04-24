import os
import yaml
from pathlib import Path

tasks_dir = Path(r"e:\Jasper\My_Projact\eos-sc-monorepo\sc-project\data-monitor\tasks")

def update_task(file_path):
    if file_path.name == "_template.md":
        return
    
    content = file_path.read_text(encoding="utf-8")
    
    # Parse frontmatter
    if content.startswith("---"):
        end_pos = content.find("---", 3)
        if end_pos != -1:
            frontmatter_text = content[3:end_pos]
            body = content[end_pos+3:]
            try:
                frontmatter = yaml.safe_load(frontmatter_text)
                if not isinstance(frontmatter, dict): frontmatter = {}
            except:
                frontmatter = {}
        else:
            frontmatter = {}
            body = content
    else:
        frontmatter = {}
        body = content

    # Standardize frontmatter
    # Replace db_prefix if exists with db_host
    if "db_prefix" in frontmatter:
        prefix = frontmatter.pop("db_prefix")
        frontmatter["db_host"] = f"{prefix}_DB_HOST"
    
    if "db_host" not in frontmatter:
        frontmatter["db_host"] = "EOS_DB_HOST"
        
    if "schedule" not in frontmatter:
        frontmatter["schedule"] = "0 9 * * 1-5"
        
    # Ensure mandatory fields
    if "max_turns" not in frontmatter: frontmatter["max_turns"] = 15
    if "budget" not in frontmatter: frontmatter["budget"] = 0.5

    # Check for "数据库约束" in body
    if "数据库约束" not in body:
        constraints = "\n## 数据库约束\n- 权限要求：仅限只读，禁止执行 DML/DDL 操作。\n- 安全准则：严禁修改任何数据或表结构。\n"
        # Find first ## header or insert at top
        first_header_pos = body.find("##")
        if first_header_pos != -1:
            body = body[:first_header_pos] + constraints + "\n" + body[first_header_pos:]
        else:
            body = constraints + "\n" + body

    # Write back
    new_frontmatter = yaml.dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
    new_content = f"---\n{new_frontmatter}\n---\n\n{body.strip()}\n"
    file_path.write_text(new_content, encoding="utf-8")
    print(f"Updated {file_path.name}")

for task_file in tasks_dir.glob("*.md"):
    update_task(task_file)
