import re
import json

def parse_commands(filepath):
    with open(filepath, 'r') as f:
        lines = f.readlines()
    
    commands = {}
    i = 0
    
    while i < len(lines):
        line = lines[i]
        
        # Found a command decorator
        if '@bot.tree.command()' in line:
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
        
    # Generate index
    commands = parse_commands('../Discord_Commands.py')
    with open('../command_index.json', 'w') as f:
        json.dump(commands, indent=2, fp=f)