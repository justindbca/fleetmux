# fleetmux

[![tests](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml/badge.svg)](https://github.com/justindbca/fleetmux/actions/workflows/tests.yml)

[English](README.md) · [中文](README.zh-CN.md) · [Français](README.fr.md) · **Español**

**Gestiona equipos de agentes de IA como gestionas contenedores.** Un equipo, no un único asistente: fleetmux ejecuta un agente supervisor y N agentes workers como sesiones de
terminal visibles. El supervisor solo evalúa y reparte el trabajo; los workers solo ejecutan; quien decide cuándo está
terminado el trabajo es un daemon, no los modelos. Una sesión que se acerca a su límite de contexto cede el relevo a una
sesión nueva mediante una nota de traspaso escrita, de modo que una misma tarea puede durar horas a lo largo de muchas
sesiones de modelo. **Claude Code** oficial, **Codex CLI** oficial, **Pi** (modelos de Codex y modelos locales) y
**opencode** conviven en una misma flota, en cualquier combinación supervisor × worker que haya superado una prueba de humo
real (`fleetmux/config/fleet.yaml#cli_pairs`).

**En uso diario.** Desde octubre de 2026 mantiene flotas de servicio residentes y flotas de tareas programadas en tres Mac
para una operación de trading real: un equipo de operaciones que arranca y detiene a los demás equipos, una mesa de trading,
un equipo de revisión de estrategias que responde a disparadores de precio e indicadores, y un equipo de investigación
programado. Cada equipo tiene un líder de IA y workers de IA; el equipo de operaciones dirige la mesa de trading y el equipo
de revisión como subflotas suyas.

## Un jefe de proyecto, no un secretario

Los agentes tipo asistente personal ya pueden lanzar subagentes, ejecutar tareas programadas y pasar de un modelo a otro
ante un fallo, pero el resultado de un subagente sigue volviendo a un modelo para que lo juzgue, las tareas largas dependen
de la compactación del contexto y el modelo es el que indique quien llama o la configuración. fleetmux añade lo que hace un
jefe de proyecto:

1. **Entregas aceptadas por el programa.** Si los entregables existen, si contienen lo que deben y si las pruebas pasan lo
   decide el daemon, no se toma del informe del propio modelo.
2. **Sesiones que continúan sin fin.** Cerca del límite de contexto, una sesión escribe una nota de traspaso y una sesión
   nueva ocupa su lugar; una sesión congelada se reinicia en el sitio; el daemon y el supervisor se vigilan mutuamente.
3. **Reparto según una matriz de capacidades.** Antes de cada envío, el asesor elige el modelo y el nivel de esfuerzo a partir
   de una matriz de capacidades (dominio × tipo de tarea × modelo, cada celda respaldada por evidencia), y la elección debe
   superar una comprobación contra esa matriz; un límite de uso hace pasar la ranura al siguiente modelo utilizable de la
   cadena de su rol, o pone la tarea en pausa.
4. **Visible en todo momento.** Cada agente es una sesión de terminal visible que puedes observar y en la que puedes
   intervenir cuando quieras.
5. **CLI oficiales, tu propio inicio de sesión.** Cada agente se ejecuta en la CLI de su proveedor con tu propio inicio de
   sesión; no se extrae ningún token, no se suplanta ningún cliente y ninguna llamada a la API pasa por un proxy.
6. **Un presupuesto por tarea.** Una tarjeta de presupuesto limita llamadas, rondas, horas y cuota por cuenta; una tabla de
   límites de uso baja un nivel de modelo o pausa la ejecución para reanudarla más tarde.
7. **Equipos de equipos.** Un worker puede convertirse en el líder de su propia subflota, así que una sola flota residente
   puede llevar varias líneas de negocio, cada una arrancada y detenida según su propio horario.

Aprendimos por qué esto importa con nuestra propia instalación: una pasarela de asistente que teníamos en marcha perdió sus
canales de chat durante meses y nada desde fuera lo detectó, porque nada desde fuera lo estaba comprobando. En una flota, el
daemon comprueba cada entrega.

Creado por Justin Wang (Toronto), ingeniero de sistemas de IA: [LinkedIn](https://www.linkedin.com/in/justinwangto) · [X](https://x.com/alaboluo).
Disponible para trabajos freelance: puesta en marcha de entornos de IA desde cero, ajuste de entornos de ejecución de
agentes, aprovechamiento de las capacidades de los LLM y orquestación multiagente. Escríbeme por mensaje directo en X.

[Referencia de diseño](docs/design.md) (en inglés)

**Puntos destacados**

- **Aceptación literal.** Una tarea solo termina cuando los entregables declarados existen y contienen lo que deben; las
  declaraciones `COMPLETED` falsas se cuentan y bloquean la tarea.
- **Rotación de contexto.** Al llegar a una proporción configurable de la ventana de contexto, la sesión escribe una nota de
  traspaso y un sucesor (`worker-1` → `worker-1.1`) continúa en la misma pestaña.
- **Un Redis privado por ejecución** (solo socket Unix, sin TCP) que transporta un bus de mensajes acotado con entregas
  verificadas por generación, de modo que una llamada de retorno tardía de una sesión retirada nunca puede actuar.
- **Vigilancia y freno.** Detección de bloqueos con cinco estados por ranura; una tarjeta de presupuesto por tarea (llamadas,
  rondas, horas, cuota por cuenta) y una tabla de límites de uso que baja una ranura bloqueada por su cadena de modelos o
  pausa la ejecución de forma reanudable. Las cuentas viven en el bloque `accounts` de tu copia privada del archivo de
  configuración, nunca en el código.
- **Un solo archivo de configuración.** Cada tabla (pares de CLI, modos de ciclo de vida, límites, diálogos de vigilancia,
  fichas de modelos, perfiles, cuentas) es un bloque de `fleetmux/config/fleet.yaml`; un `~/.fleetmux/fleet.yaml` privado
  con el mismo formato sustituye los bloques que un operador puede cambiar.
- **CLI oficiales, tu propio inicio de sesión.** Claude Code siempre se ejecuta como la TUI oficial `claude` con tu propio
  inicio de sesión, y Pi nunca recibe credenciales de Claude; los modelos de Codex se ejecutan en la Codex CLI oficial o a
  través del propio inicio de sesión de OpenAI de Pi. fleetmux no extrae ni reenvía tokens, no suplanta ningún cliente y no
  pasa llamadas a la API por un proxy.
- **Subflotas.** Un worker puede convertirse en el supervisor de su propia flota hija.
- **Más de 2000 pruebas** (Redis en memoria, cmux simulado, transcripciones sintéticas; Redis real donde importa).

```
 cmux workspace "Fleet agents" (one page)
 ┌───────────────────────────────┬────────────────────────┐
 │ supervisor-1.2                │ [worker-1.1] [worker-2] │
 │ supervisor TUI (selected)     │ worker tabs            │
 │                               │                        │
 ├───────────────────────────────┤ selected worker's TUI  │
 │ [history] [daemon]            │                        │
 │ retired screens + handoffs    │                        │
 └───────────────────────────────┴────────────────────────┘
 daemon: the fleet daemon, private Redis socket, routing, verification and watchdog.
```

A la izquierda, la TUI del supervisor (seleccionada); a la derecha, las pestañas de los workers y, debajo, la TUI del worker
seleccionado; abajo a la izquierda, las pestañas `history` (pantallas retiradas y notas de traspaso) y `daemon` (el daemon de
la flota: socket Redis privado, enrutamiento, verificación y vigilancia).

## Casos de uso

Cada `start` indica un `--lifecycle`, que decide qué significa «terminado» y cómo se cierra la ejecución
(`fleetmux/config/fleet.yaml#lifecycle_modes`). Estos son los escenarios para los que está pensado; el informe
programado, el servicio residente y el desarrollo de funcionalidades funcionan a diario.

- **Operaciones que solo pueden usar modelos locales.** Algunos entornos solo permiten modelos que se ejecutan en tus propias
  máquinas, y a los modelos locales se les da mal trabajar juntos: pierden la cuenta de quién hace qué, dan por hecho un
  trabajo que no lo está, desbordan una ventana de contexto pequeña o se congelan a mitad de turno. Parte del problema es el
  modelo y parte es el entorno de ejecución que lo rodea. fleetmux quita la colaboración a los modelos y se la da al daemon.
  El supervisor solo elige el siguiente paso y un worker solo lo lleva a cabo; el daemon decide cuándo ha terminado un turno,
  si el resultado existe y dice lo que debe (archivos comprobados literalmente, respuestas de servicio contrastadas con
  `reply.required`; tres declaraciones `COMPLETED` falsas bloquean la tarea), cuándo una sesión cede el relevo a una nueva
  (ventana y proporción por modelo) y cuándo se reinicia una sesión congelada. Supervisor y workers pueden ser todos modelos
  locales a través de Pi (`--supervisor-cli pi --worker-cli pi --pi-model <provider>/<model>`); `--max-busy` mantiene las
  peticiones simultáneas por debajo del límite de memoria del servidor de inferencia; el Redis privado de la flota solo
  escucha en un socket Unix y no abre ningún puerto.
- **Vigilancia de trading: estrategias confiadas a la IA, revisadas al dispararse.** Tu propio programa vigila precios e
  indicadores; no se consulta al modelo durante todo el día. Cuando se cruza un nivel de precio o un parámetro de un
  indicador, el programa envía una petición, por ejemplo `{"to": "exit-1", "payload": {strategy, position, trigger, data},
  "reply": {"required": ["verdict", "param", "evidence"]}}`, y el worker que tiene ese rol (un prompt por rol mediante
  `--roles`, sus herramientas y skills mediante `--worker-kit`) hace una revisión completa: lee la posición y los datos de
  mercado, vuelve a comprobar los supuestos de la estrategia y responde en JSON. Una petición que indica `to` se salta el
  turno del supervisor; los disparadores que llegan a la vez se encolan bajo `--max-busy`; el daemon comprueba los campos de
  la respuesta antes de que tu programa la lea (`fleetmux results --drain`). Las peticiones no tienen un plazo global: una
  que caduca solo retiene su propia ranura, y un worker de Claude Code congelado reanuda su propia sesión y vuelve a recibir la
  misma petición. La flota no lanza órdenes ni
  conoce ningún calendario: tu programa decide qué hacer con el veredicto, pasa cualquier orden por sus propios controles y
  aplica `pause` / `resume` a la flota en torno al horario de mercado.
- **Un modelo de capacidad media al frente de un grupo de modelos gratuitos más débiles para el trabajo rutinario.**
  Traducir, redactar, resumir y clasificar es sobre todo cuestión de volumen. Un supervisor de capacidad media (a través de
  Pi o Codex) divide el trabajo en piezas pequeñas, un capítulo, un lote de documentos, un tema, y declara los archivos que la
  tarea debe producir, la extensión de cada uno y lo que cada uno debe contener. Workers con modelos gratuitos a través de Pi
  hacen las piezas (un modelo por fila del roster, `pi_model:`). Un modelo débil solo ve una pieza pequeña cada vez:
  `"fresh": true` en un elemento de reparto abre una sesión limpia en la misma ranura para que la pieza anterior no se cuele
  en la siguiente, y una pieza larga cede el relevo a una sesión nueva antes de que se desborde una ventana pequeña. Lo que un
  modelo débil hace mal lo detecta el programa en lugar de darlo por bueno: la tarea solo se cierra cuando cada entregable
  declarado existe, es lo bastante largo y contiene el texto requerido, y tres declaraciones `COMPLETED` falsas la bloquean.
  Los endpoints gratuitos son inestables y tienen tope: los límites de tasa y de uso pasan por la tabla de límites, que baja
  la ranura por su cadena de modelos o pausa la ejecución de forma reanudable; `--max-busy` se mantiene por debajo de la
  concurrencia del plan gratuito; los workers se añaden o se quitan editando el YAML del roster mientras la flota está en
  marcha. Para una pasada de calidad, asigna a una fila del roster un `pi_model:` más potente y úsala como revisora.
- **Informe de investigación programado** (`scheduled`). El programador de tareas de quien llama arranca una flota a horas
  fijas del día hábil (con `clock_gate` activado, la propia flota se niega a arrancar en un día de mercado cerrado): un
  supervisor y dos workers de Claude Code con skills de investigación y un MCP de búsqueda web a través de `--worker-kit`. El
  objetivo declara el archivo del informe y las secciones que debe contener; el daemon lo acepta de forma literal, impone a la
  ejecución un presupuesto de tiempo estricto y cierra el espacio de trabajo al terminar.
- **Servicio de evaluación residente** (`service`). Otro programa envía peticiones JSON con `fleetmux request`; una
  petición que indica `to` va directamente al worker de ese rol (`--worker-names exit-1,reentry-1,...`, un prompt por rol
  mediante `--roles`). El daemon comprueba cada respuesta frente a `reply.required` / `reply.allowed`, y quien llama detiene
  la flota fuera de su horario de trabajo con `pause` / `resume`.
  Consulta [Service mode](README.md#service-mode-resident-fleet) en el README en inglés.
- **Desarrollo de funcionalidades con revisión independiente** (`dev_task`). El supervisor divide la funcionalidad, los
  workers la construyen y workers revisores independientes la evalúan; la revisión se limita a tres rondas y los P0 / P1 que
  queden pasan a ti en lugar de abrir una cuarta ronda. La tarea solo está terminada cuando el `test_cmd` de la puerta de
  entrega ha pasado y el trabajo está fusionado y entregado a la cola de publicación. Los contratos de interfaz
  (`"contracts": true`) congelan las interfaces compartidas antes de empezar.
- **Programa en varias fases** (`program`). Cada fase se ejecuta como su propia flota hija `dev_task`; un registro anota las
  fases y el programa termina cuando la última fase está fusionada y revisada.
- **Revisar hasta aprobar** (`review_loop`). Revisión → corrección → revisión, como máximo tres rondas, y después un informe.
- **Una tarea que se le queda grande a un worker** (en desarrollo). Cuando un worker mantiene ocupado un flujo de trabajo más
  allá de un tiempo configurable, se le pide que proponga una división; con la aprobación del supervisor (y los modelos
  elegidos), el worker cede el relevo y su sucesor pasa a ser el supervisor de una flota hija, que se muestra en directo en la
  zona de workers de la flota madre. Un worker también puede pedirlo por su cuenta. Consulta
  [Sub-fleets](README.md#sub-fleets-a-worker-becomes-a-sub-fleet-supervisor-development) en el README en inglés y
  `docs/design.md` §44.
- **Una sola flota de operaciones residente para varias líneas de trabajo** (en desarrollo). Un supervisor principal en modo
  servicio tiene un worker por línea de negocio; cada uno de esos workers supervisa una flota hija con nombre declarada en el
  roster, que un programa externo puede arrancar y detener según su propio horario (`docs/design.md` §29).
- **Modelos de día y de noche.** `start --roster day.yaml --night-roster night.yaml` y `fleetmux night on|off` cambian, en
  un momento de inactividad, cada worker cuyo perfil difiere a su perfil de noche (por ejemplo, un modelo gratuito o local),
  con los mismos nombres de ranura, e informan al supervisor de lo que está hecho y lo que falta (un ciclo completo
  día → noche → día aún no está aceptado de extremo a extremo). `start --resume` recupera del mismo modo una ejecución
  interrumpida.
- **Ejecútalo en cualquier sitio, obsérvalo desde cualquier sitio.** En Linux, el backend `fleetmux` sustituye a cmux;
  `fleetmux attach` (también por `ssh -t`) o `fleetmux gui` muestra la página en directo desde otro terminal, incluida una
  aplicación de terminal en el móvil.

Los canales de chat (Telegram, Feishu, WeChat, correo electrónico, Slack) son un segundo teclado del supervisor, con
los resultados y los avisos de «esperando a una persona» devueltos al mismo chat: `fleetmux channel`,
[contracts §8](docs/contracts.md).

## Instalación

Un solo comando: `fleetmux <subcommand>` (por ejemplo `fleetmux start`, `fleetmux status`, `fleetmux channel`). El binario
ejecuta por sí mismo sus subcomandos de terminal (`server`, `attach`, `reap`, `web`, `gui --runtime D`) y pasa todos los
subcomandos de flota a la CLI de Python de la misma versión (docs/design.md §50). Una máquina instalada con el nombre
anterior del proyecto traslada sus datos una vez con `fleetmux home migrate` (docs/design.md §50.1).

```bash
git clone https://github.com/justindbca/fleetmux && cd fleetmux
pip install -e .
```

Añade un perfil de Codex para los agentes. Codex ≥ 0.156 guarda cada perfil en su propio archivo
(consulta [`codex.config.example.toml`](codex.config.example.toml)):

```toml
# ~/.codex/fleet.config.toml   (selected with `codex --profile fleet`)
model = "gpt-5.5"
```

Nada más: la flota crea su propio home de Codex para los agentes y responde ella misma, para cada directorio de agente, a la
pregunta de Codex «¿confiar en esta carpeta?».

Los requisitos (cmux, la Codex CLI, `redis-server`, versión de Python) y la instalación por versión en una máquina de destino
(`scripts/install_release.sh`) están en el README en inglés:
[Requirements](README.md#requirements) y [Release install](README.md#release-install-on-a-target-machine-macos-or-linux).

## Inicio rápido

Abre un terminal de cmux, configura tu proveedor y modelo de Pi, y sustituye `local/my-model` más abajo. Los arranques nuevos
requieren Redis v2 y una autorización explícita del modo de desarrollo. Sin `--supervisor-cli` / `--worker-cli`, `start` toma
el par por defecto de `fleetmux/config/fleet.yaml#cli_pairs`: **un supervisor Pi con `openai-codex/gpt-6-astra` (thinking
high) y workers oficiales de Claude Code**. Esa tabla enumera todos los pares admitidos y su estado (consulta
[Redis v2 development runtime](README.md#redis-v2-development-runtime) en el README en inglés); un par que no figure en ella
se rechaza. `start` también exige `--lifecycle <mode>` (`service`, `dev_task`, `program`, `scheduled`, `review_loop`,
`smoke`; consulta `fleetmux/config/fleet.yaml#lifecycle_modes`); un par `unverified` solo arranca con `--lifecycle smoke`.
Los ejemplos de configuración y arranque específicos de Codex del README en inglés se conservan únicamente como referencia
heredada de v1.

```bash
fleetmux start --workers 2 --supervisor-cli pi --worker-cli pi --lifecycle <mode> \
  --pi-model local/my-model --allow-unsandboxed-pi --allow-development-v2
echo '{"goal": "Survey the three most-cited papers on X and write a comparison",
       "hours": 4,
       "artifacts": [{"path": "worker-1/report.md", "min_chars": 800, "contains": ["Conclusion"]}]}' \
  | fleetmux task -                           # hand the goal to the supervisor
fleetmux status                               # task + per-agent state, context usage, inbox depth
fleetmux stop                                 # cancel; confirm runtime retirement before Redis shutdown
```

`fleetmux task -` entrega el objetivo al supervisor; `fleetmux status` muestra la tarea, el estado de cada agente, el uso de
contexto y la profundidad de la bandeja de entrada; `fleetmux stop` cancela y confirma la retirada del runtime antes de
apagar Redis.

## Referencia completa

Para la referencia completa, consulta el [README en inglés](README.md): requisitos, instalación por versión, backends de
terminal (cmux, WezTerm, fleetmux) y opciones de `start`, disposición y página de historial, modo servicio, roster YAML de
workers, ventana y proporción de traspaso, reanudación de ejecuciones, contratos de interfaz, subflotas, agentes de Claude
Code y Pi, runtime de desarrollo Redis v2, recorrido de una tarea, claves de Redis, estado de las pruebas, mapa de módulos,
limitaciones conocidas y desarrollo. La referencia de diseño está en [docs/design.md](docs/design.md).

Licencia: MIT
