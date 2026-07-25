#!/bin/bash
echo "Starte Ollama Aufräum- und Update-Prozess..."

echo "1. Lösche veraltete und redundante Modelle..."
ollama rm llama3:latest mistral:latest codellama:7b codellama:13b codellama:34b \
    deepseek-coder:6.7b llava:13b \
    hf.co/TheBloke/WizardLM-Uncensored-SuperCOT-StoryTelling-30B-GGUF:Q4_K_M \
    hf.co/TheBloke/MythoMax-L2-13B-GGUF:Q4_K_M \
    codestral:22b codestral:22b-v0.1-q5_K_M \
    qwen2.5-coder:0.5b qwen2.5-coder:1.5b \
    gemma4:26b gemma4:e2b \
    qwen3:32b qwen3:8b qwen3-coder:latest qwen3-vl:8b \
    hf.co/DevQuasar/Qwen.Qwen3.5-9B-GGUF:Q4_K_M hf.co/DevQuasar/Qwen.Qwen3.5-4B-GGUF:Q4_K_M \
    hf.co/huihui-ai/Huihui-Qwen3.6-35B-A3B-abliterated-MTP-GGUF:Q2_K \
    hf.co/huihui-ai/Huihui-Qwen3.6-35B-A3B-Claude-4.7-Opus-abliterated-MTP-GGUF:Q2_K \
    hf.co/TeichAI/Qwen3-14B-Claude-4.5-Opus-High-Reasoning-Distill-GGUF:Q4_K_M

echo "2. Aktualisiere wichtige bestehende Modelle..."
for model in llama3.1:8b mistral-nemo:latest minicpm-v:latest llama3.2-vision:11b deepseek-r1:14b deepseek-r1:7b qwen2.5-coder:32b gemma3:27b; do
    echo "Updating $model..."
    ollama pull $model
done

echo "3. Installiere das neue Modell (Qwen3.6-35B-A3B-Uncensored Q4_K_M)..."
ollama pull hf.co/HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive:Q4_K_M

echo "Alle Aufgaben erfolgreich abgeschlossen!"
