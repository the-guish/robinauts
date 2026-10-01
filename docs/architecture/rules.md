# Layer references rules

Anything not allowed is forbidden.

## Top level components

agent_engines -> Nothing from robinauts, only external libs
controller -> agent_engines
web -> controller


### Inside agent_engines

echo_engine -> contract
langchain_engine -> contract + langchain deps + postgres
pydantic_ai_engine -> contract + pydantic ai engine + postgres

# Inside controller

application -> contract + ports
adapters -> ports
