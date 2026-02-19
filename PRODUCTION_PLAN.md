# Plano de Geração de Assets - Produção

## Status Atual

### Poses Existentes (14)
- explaining_hand_up, frustrated, fullbody, neutral_arms_crossed, pointing
- pushing_pose, relieved_exhale, shaking_no, smiling, surprised
- thinking_hand_chin, worried

### Props Existentes (30)
- airplane, brain, briefcase, broken_fridge, button_generic, calendar, car
- chart_down, chart_up, coffee, coin_stack, contract, debt_pile, down_arrow
- fire, green_check, house, moneybag, padlock, paper_stack, phone_balance
- phone_blank, piggy_bank, question_mark, ramen, receipt, red_x, savings_jar
- up_arrow, warning_sign

---

## Plano de Geração

### 🎭 Novas Poses (1 faltando)
Usar Seedream 4.5 Edit com referência `smiling.png` como base:

| # | Pose | Status |
|---|------|--------|
| 1 | celebrating_victory | ✅ Feito |
| 2 | thinking_calculator | ✅ Feito |
| 3 | presenting_chart | ✅ Feito |
| 4 | shrugging_doubt | ✅ Feito |
| 5 | facepalm_disappointed | ✅ Feito |
| 6 | thumbs_up | ✅ Feito |
| 7 | shocked_face | ✅ Feito |
| 8 | handshake_deal | ⏳ Pendente |

### 📦 Novos Props (20 faltando)
Usar GPT-4o (text-to-image):

| # | Prop | Prompt | Prioridade |
|---|------|--------|------------|
| 1 | laptop_with_chart | Laptop showing green stock chart | ✅ Feito (GPT-4o) |
| 2 | credit_card | Credit card, flat design | Alta |
| 3 | bank_building | Bank/financial building icon | Alta |
| 4 | stock_chart_graph | Stock chart with upward trend | Alta |
| 5 | bitcoin_symbol | Bitcoin coin logo | Alta |
| 6 | dollar_sign | Golden dollar sign | Média |
| 7 | percentage_up | Green percentage going up | Média |
| 8 | trend_line_down | Red stock trend going down | Média |
| 9 | invoice_document | Invoice/receipt document | Média |
| 10 | safe_box | Safe/treasure box | Média |
| 11 | alarm_clock | Alarm clock, urgent | Baixa |
| 12 | trophy_medal | Gold trophy/medal | Baixa |
| 13 | target_bullseye | Target with bullseye | Baixa |
| 14 | wallet | Wallet with money | Baixa |
| 15 | contract_signed | Document with checkmark | Baixa |
| 16 | phone_stock_app | Smartphone showing stock app | Baixa |
| 17 | coins_rain | Coins falling | Baixa |
| 18 | rocket_launch | Rocket taking off | Baixa |
| 19 | pyramid_scheme | Pyramid diagram | Baixa |
| 20 | gift_box | Gift/bonus box | Baixa |

---

## Estrutura de Pasta Final

```
assets/
├── poses/
│   ├── existing/           # 14 poses do Whisk
│   ├── celebrating_victory/ # Feito
│   ├── thinking_calculator/ # Pendente
│   ├── presenting_chart/    # Pendente
│   └── ...
├── props/
│   ├── existing/           # 30 props do Whisk
│   ├── laptop_with_chart/  # Feito
│   ├── credit_card/        # Pendente
│   └── ...
```

---

## Método de Geração

### Poses → Seedream 4.5 Edit
- Referência: `smiling.png` (catbox)
- Modelo: `seedream/4.5-edit`
- Hospedagem: catbox.moe

### Props → GPT-4o
- Modelo: gpt-image-1
- Estilo: Family Guy animation, thick black outlines
- Custo: ~$0.04/imagem
