variable "node_name" {
  type        = string
  default     = "edge-01"
  description = "Имя ноды (Hetzner server name)."
}

variable "server_type" {
  type        = string
  default     = "cx23" # cheapest (€3.99/мес, x86). ARM = cax11 (€4.49).
  description = "Тип сервера Hetzner."
}

variable "location" {
  type        = string
  default     = "fsn1" # fsn1=Falkenstein, nbg1=Nuremberg, hel1=Helsinki — все EU.
  description = "Локация (EU)."
}

variable "image" {
  type        = string
  default     = "ubuntu-24.04"
  description = "ОС-образ."
}

variable "ssh_public_key" {
  type        = string
  description = "Публичный SSH-ключ для root при первом входе (потом bootstrap заведёт devops)."
}

variable "node_role" {
  type        = string
  default     = "edge-gateway" # edge-gateway | geo-exit
  description = "Роль ноды (label)."
}
