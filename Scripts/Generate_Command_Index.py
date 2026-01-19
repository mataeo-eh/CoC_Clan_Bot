import re
import json
from pathlib import Path

def find_view_class(lines, view_name):
    """Find the line range for a View class definition and its parent class if it has one"""
    class_pattern = re.compile(rf'class {view_name}\(')
    
    for i, line in enumerate(lines):
        if class_pattern.search(line):
            start_line = i + 1  # 1-indexed
            
            # Extract parent class from the class definition
            # Pattern: class ViewName(ParentClass): or class ViewName(ParentClass, ...):
            parent_match = re.search(rf'class {view_name}\(([^,)]+)', line)
            parent_class = None
            if parent_match:
                potential_parent = parent_match.group(1).strip()
                # Only consider it a parent if it's not a standard base class
                if potential_parent not in ('discord.ui.View', 'View', 'discord.ui.Modal', 'Modal'):
                    parent_class = potential_parent
            
            # Find end of class (next class definition or dedent to column 0)
            indent_level = len(line) - len(line.lstrip())
            end_line = len(lines)
            
            for j in range(i + 1, len(lines)):
                # Found another class at same or lower indent level
                if lines[j].startswith('class ') or (lines[j].strip() and not lines[j].startswith(' ' * (indent_level + 1))):
                    if not lines[j].strip().startswith('#'):
                        end_line = j
                        break
            
            return {
                "start_line": start_line,
                "end_line": end_line,
                "parent_class": parent_class
            }
    
    return None

def parse_commands(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    commands = {}
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        if '@bot.tree.command' in line:
            decorator_line = i
            
            # Next line should be the function definition
            i += 1
            while i < len(lines) and not lines[i].strip().startswith('async def'):
                i += 1
            
            if i >= len(lines):
                break
                
            # Extract command name from function definition
            func_match = re.search(r'async def (\w+)', lines[i])
            if func_match:
                cmd_name = func_match.group(1)
                start_line = decorator_line + 1  # 1-indexed for view tool
                
                # Find end of function
                # First, skip past the function signature (might be multi-line)
                i += 1
                while i < len(lines) and ')' not in lines[i]:
                    i += 1
                # Now skip the line with the closing ) and :
                if i < len(lines) and ')' in lines[i]:
                    i += 1
                # NOW get the indent level from the actual function body
                indent_level = len(lines[i]) - len(lines[i].lstrip()) if i < len(lines) else 0
                
                end_line = len(lines)
                while i < len(lines):
                    if '@bot.tree.command' in lines[i]:
                        end_line = i
                        break
                    if lines[i].strip() and not lines[i].startswith(' ' * indent_level):
                        if not lines[i].strip().startswith('#'):
                            end_line = i
                            break
                    i += 1
                
                # Look for ALL View instantiations within this command
                # Match pattern: view = AnyClassName(
                view_classes = []
                for line_num in range(start_line - 1, min(end_line, len(lines))):
                    view_match = re.search(r'view\s*=\s*([A-Z]\w+)\s*\(', lines[line_num])
                    if view_match:
                        view_classes.append(view_match.group(1))
                
                command_info = {
                    "start_line": start_line,
                    "end_line": end_line
                }
                
                # If we found View(s), locate their class definitions
                if view_classes:
                    command_info["view_classes"] = []
                    for view_class_name in view_classes:
                        view_info = find_view_class(lines, view_class_name)
                        if view_info:
                            view_data = {
                                "name": view_class_name,
                                "start_line": view_info["start_line"],
                                "end_line": view_info["end_line"]
                            }
                            
                            # If this view has a parent class, find it too
                            if view_info.get("parent_class"):
                                parent_info = find_view_class(lines, view_info["parent_class"])
                                if parent_info:
                                    view_data["parent_class"] = {
                                        "name": view_info["parent_class"],
                                        "start_line": parent_info["start_line"],
                                        "end_line": parent_info["end_line"]
                                    }
                            
                            command_info["view_classes"].append(view_data)
                
                commands[cmd_name] = command_info
                
                # If we found another decorator, don't increment i
                if i < len(lines) and '@bot.tree.command' in lines[i]:
                    continue
        
        i += 1
    
    return commands

# Get project root
script_dir = Path(__file__).parent
project_root = script_dir.parent

# Generate index
commands_file = project_root / 'Discord_Commands.py'
output_file = project_root / 'command_index.json'

commands = parse_commands(commands_file)
with open(output_file, 'w') as f:
    json.dump(commands, indent=2, fp=f)

print(f"Generated index with {len(commands)} commands")
commands_with_views = sum(1 for cmd in commands.values() if "view_classes" in cmd)
total_views = sum(len(cmd.get("view_classes", [])) for cmd in commands.values())
total_parents = sum(
    sum(1 for v in cmd.get("view_classes", []) if "parent_class" in v) 
    for cmd in commands.values()
)
print(f"{commands_with_views} commands have View classes ({total_views} total views)")
print(f"{total_parents} views have parent classes")