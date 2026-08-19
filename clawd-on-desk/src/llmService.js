"use strict";

const { search } = require("duck-duck-scrape");

const SYSTEM_PROMPT = `You are a helpful desktop pet assistant.
You have access to the following tools:

<tools>
<tool name="get_current_time" description="Get the current local time" />
<tool name="get_weather" description="Get weather for a location">
  <param name="location" type="string" />
</tool>
<tool name="web_search" description="Search the web">
  <param name="query" type="string" />
</tool>
</tools>

To use a tool, output exactly:
<call name="tool_name">{"param":"value"}</call>

You may only use one tool per response. Do not add any text before or after the tool call when you use a tool.
If you don't need a tool, just answer normally.`;

async function executeTool(name, argsStr) {
  let args = {};
  try {
    if (argsStr) {
      args = JSON.parse(argsStr);
    }
  } catch (e) {
    return "Error parsing tool arguments.";
  }

  switch (name) {
    case "get_current_time":
      return new Date().toString();
    case "get_weather": {
      if (!args.location) return "Missing location.";
      try {
        const res = await fetch(`https://wttr.in/${encodeURIComponent(args.location)}?format=j1`);
        const data = await res.json();
        return `Current condition: ${data.current_condition[0].temp_C}°C, ${data.current_condition[0].weatherDesc[0].value}`;
      } catch (e) {
        return "Failed to fetch weather.";
      }
    }
    case "web_search": {
      if (!args.query) return "Missing query.";
      try {
        const res = await search(args.query, { safeSearch: "off" });
        if (!res.results || res.results.length === 0) return "No results found.";
        return res.results.slice(0, 3).map(r => `${r.title}: ${r.description}`).join("\n");
      } catch (e) {
        return "Failed to search the web.";
      }
    }
    default:
      return `Tool ${name} not found.`;
  }
}

async function runChatLoop(messages, onDelta, maxNewTokens, temperature, top_p, top_k, repetition_penalty) {
  // We inject the system prompt into the first message or prepend it
  const messagesToSend = [];
  let systemInjected = false;
  for (const m of messages) {
    if (m.role === "system") {
      messagesToSend.push({ role: "system", content: SYSTEM_PROMPT + "\n\n" + m.content });
      systemInjected = true;
    } else {
      messagesToSend.push(m);
    }
  }
  if (!systemInjected) {
    messagesToSend.unshift({ role: "system", content: SYSTEM_PROMPT });
  }

  while (true) {
    let responseText = "";
    
    try {
      const resp = await fetch("http://127.0.0.1:18766/v1/chat/completions", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          messages: messagesToSend,
          stream: true,
          max_tokens: maxNewTokens || 768,
          temperature: typeof temperature === "number" ? temperature : 0.6,
          top_p: typeof top_p === "number" ? top_p : 0.95,
          top_k: typeof top_k === "number" ? top_k : 0,
          repetition_penalty: typeof repetition_penalty === "number" ? repetition_penalty : 1.05
        })
      });

      if (!resp.ok) {
        throw new Error(`LLM server error: ${resp.status}`);
      }

      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      
      let isToolCall = false;
      let firstChunk = true;

      // Stream handling
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const chunk = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          if (chunk.startsWith("data: ")) {
            const dataStr = chunk.slice(6);
            if (dataStr === "[DONE]") break;
            try {
              const data = JSON.parse(dataStr);
              const delta = data.choices[0].delta.content;
              if (delta) {
                responseText += delta;
                
                if (firstChunk) {
                   if (responseText.trim().startsWith("<call")) {
                       isToolCall = true;
                   }
                   firstChunk = false;
                }
                
                // Only send to UI if it's not a tool call
                if (!isToolCall) {
                  onDelta({ content: delta, event: "delta" });
                }
              }
            } catch (e) {}
          }
        }
      }

      // Check for tool call
      const textTrim = responseText.trim();
      const match = textTrim.match(/<call\s+name="([^"]+)">([\s\S]*?)<\/call>/);
      
      if (match) {
        const toolName = match[1];
        const toolArgs = match[2];
        onDelta({ content: " (Running tool " + toolName + "...) ", event: "delta" });
        const result = await executeTool(toolName, toolArgs);
        
        messagesToSend.push({ role: "assistant", content: responseText });
        messagesToSend.push({ role: "user", content: `<response>${result}</response>` });
        
        // Loop continues, hitting the LLM again
      } else {
        // No tool call, this is the final answer
        break;
      }
      
    } catch (err) {
      throw err;
    }
  }
}

module.exports = {
  runChatLoop
};
