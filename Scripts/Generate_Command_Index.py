import re
import json
from pathlib import Path

def parse_commands(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    commands = {}
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Found a command decorator
        if '@bot.tree.command(' in line:
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
                
                # Find end of function (next decorator or end of file)
                end_line = start_line
                i += 1
                indent_level = len(lines[i]) - len(lines[i].lstrip())
                
                while i < len(lines):
                    if '@bot.tree.command()' in lines[i]:
                        end_line = i
                        break
                    # Check if we've dedented (function ended)
                    if lines[i].strip() and not lines[i].startswith(' ' * indent_level):
                        if not lines[i].strip().startswith('#'):
                            end_line = i
                            break
                    i += 1
                else:
                    end_line = len(lines)
                
                commands[cmd_name] = {
                    "start_line": start_line,
                    "end_line": end_line
                }
        
        i += 1
    
    return commands


if __name__ == "__main__":
        
    # Get project root (Scripts/../ = project root)
    script_dir = Path(__file__).parent
    project_root = script_dir.parent

    # Generate index
    commands_file = project_root / 'Discord_Commands.py'
    output_file = project_root / 'command_index.json'

    commands = parse_commands(commands_file)
    with open(output_file, 'w') as f:
        json.dump(commands, indent=2, fp=f)

    print(f"Generated index with {len(commands)} commands")