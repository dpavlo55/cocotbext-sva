# Supported SVA


| Area | Supported |
|---|---|
| Sequences | `##n`, `##[m:n]`, `##[m:$]`, `##[*]`, `##[+]`, `[*n]`, `[*m:n]`, `[*]`, `[+]`, `[->n]`, `[=n]`, `and`, `or`, `intersect`, `within`, `throughout`, `first_match`, local variables `(seq, v = e)`, `.triggered` / `.ended` |
| Properties | `|->`, `|=>`, `#-#`, `#=#`, `not`, `and`, `or`, `implies`, `iff`, `if`/`else`, `case`, `strong()`, `weak()`, `disable iff`, `nexttime`, `s_nexttime`, `always`, `s_always`, `eventually`, `s_eventually` (with ranges), `until`, `s_until`, `until_with`, `s_until_with`, `accept_on`, `reject_on`, `sync_accept_on`, `sync_reject_on` |
| Functions | `$past(e, n, gate)`, `$rose`, `$fell`, `$stable`, `$changed`, `$sampled`, `$onehot`, `$onehot0`, `$countones`, `$countbits`, `$isunknown`, `$signed`, `$unsigned`, `$bits`, `$clog2` |
| Expressions | SV operators (including `->` and `<->`) with SV width and sign rules, bit/part selects (VHDL `to`/`downto` ranges respected), concatenation, replication |
| Directives | `assert property`, `assume property`, `cover property` (each also as a single attempt: `initial=True`), `expect`, immediate and deferred immediate assert/assume/cover |
| Clocks | one clocking event per assertion (`posedge`, `negedge`, `edge`, `iff`), or several inside the property (multi-clock sequences and properties) |
