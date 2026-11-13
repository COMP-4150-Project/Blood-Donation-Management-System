# Vytal live demo

Blood donation management system (COMP-4150). Flask GUI on PostgreSQL 16. All data is made up.

## Open it
1. On GitHub press **Code → Codespaces → Create codespace on main**.
2. Wait about 3 minutes for the first build. The app opens by itself on port 5050.
3. To share the link: open the **Ports** tab, right-click port 5050, set **Port visibility → Public**, and copy the URL.

The Codespace stops after inactivity, so the link only works while it is running. Reopen the Codespace to bring it back.

## Sign in
The sign-in and sign-up pages list every account. All of them use the password `Vytal-demo-2026`.

| Role | Email | Sees |
|---|---|---|
| Admin | admin@vytal.example.ca | accounts, facilities, clinical rules, audit log, lookback |
| Staff | m.ross@riverside.example.ca | stock, requests, donors, tests (Riverside General) |
| Donor | liam.tremblay@mail.ca | eligibility, history, calls, screening |

## Run it on your own machine
```
createdb vytal && ./setup.sh
VYTAL_DEMO=1 PGHOST=localhost ./run_gui.sh     # http://127.0.0.1:5050
```
